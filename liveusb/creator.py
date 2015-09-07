# -*- coding: utf-8 -*-
#
# Copyright © 2008-2015  Red Hat, Inc. All rights reserved.
# Copyright © 2008-2015  Luke Macken <lmacken@redhat.com>
#
# This copyrighted material is made available to anyone wishing to use, modify,
# copy, or redistribute it subject to the terms and conditions of the GNU
# General Public License v.2.  This program is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY expressed or implied, including the
# implied warranties of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.  You should have
# received a copy of the GNU General Public License along with this program; if
# not, write to the Free Software Foundation, Inc., 51 Franklin Street, Fifth
# Floor, Boston, MA 02110-1301, USA. Any Red Hat trademarks that are
# incorporated in the source code or documentation are not subject to the GNU
# General Public License and may only be used or replicated with the express
# permission of Red Hat, Inc.

"""
Our main LiveUSBCreator module.

This contains the LiveUSBCreator parent class, which is an abstract interface
that provides platform-independent methods.  Platform specific implementations
include the LinuxLiveUSBCreator and the WindowsLiveUSBCreator.
"""

import subprocess
import tempfile
import logging
import hashlib
import shutil
import signal
import time
import os
import re
import sys

from StringIO import StringIO
from datetime import datetime
from pprint import pformat
from stat import ST_SIZE

from liveusb.releases import releases
from liveusb import _


class LiveUSBError(Exception):
    """ A generic error message that is thrown by the LiveUSBCreator """
    def __init__(self, fullMessage, shortMessage=""):
        self.args = [fullMessage]
        if shortMessage != "":
            self.short = shortMessage
        else:
            self.short = fullMessage


class LiveUSBCreator(object):
    """ An OS-independent parent class for Live USB Creators """

    iso = None          # the path to our live image
    label = "LIVE"      # if one doesn't already exist
    fstype = None       # the format of our usb stick
    drives = {}         # {device: {'label': label, 'mount': mountpoint}}
    overlay = 0         # size in mb of our persisten overlay
    dest = None         # the mount point of of our selected drive
    uuid = None         # the uuid of our selected drive
    pids = []           # a list of pids of all of our subprocesses
    output = StringIO() # log subprocess output in case of errors
    totalsize = 0       # the total size of our overlay + iso
    isosize = 0         # the size of the selected iso
    _drive = None       # mountpoint of the currently selected drive
    mb_per_sec = 0      # how many megabytes per second we can write
    log = None
    ext_fstypes = set(['ext2', 'ext3', 'ext4'])
    valid_fstypes = set(['vfat', 'msdos']) | ext_fstypes

    drive = property(fget=lambda self: self.drives[self._drive] if self._drive and len(self.drives) else None,
                     fset=lambda self, d: self._set_drive(d))

    def __init__(self, opts):
        self.opts = opts
        self._setup_logger()

    def _setup_logger(self):
        self.log = logging.getLogger(__name__)
        level = logging.INFO
        if self.opts.verbose:
            level = logging.DEBUG
        self.log.setLevel(level)
        self.handler = logging.StreamHandler()
        self.handler.setLevel(level)
        formatter = logging.Formatter("[%(module)s:%(lineno)s] %(message)s")
        self.handler.setFormatter(formatter)
        self.log.addHandler(self.handler)

    def detect_removable_drives(self, callback=None):
        """ This method should populate self.drives with removable devices """
        raise NotImplementedError

    def verify_filesystem(self):
        """
        Verify the filesystem of our device, setting the volume label
        if necessary.  If something is not right, this method throws a
        LiveUSBError.
        """
        raise NotImplementedError

    def get_free_bytes(self, drive=None):
        """ Return the number of free bytes on a given drive.

        If drive is None, then use the currently selected device.
        """
        raise NotImplementedError

    def extract_iso(self):
        """ Extract the LiveCD ISO to the USB drive """
        raise NotImplementedError

    def verify_iso_md5(self):
        """ Verify the MD5 checksum of the ISO """
        raise NotImplementedError

    def install_bootloader(self):
        """ Install the bootloader to our device.

        Platform-specific classes inheriting from the LiveUSBCreator are
        expected to implement this method to install the bootloader to the
        specified device using syslinux.  This specific implemention is
        platform independent and performs sanity checking along with adding
        OLPC support.
        """
        if not os.path.exists(os.path.join(self.dest, 'isolinux')):
            raise LiveUSBError('extract_iso must be run before '
                               'install_bootloader')
        if self.opts.xo:
            self.setup_olpc()

    def setup_olpc(self):
        """ Install the Open Firmware configuration for the OLPC.

        This method will make the selected device bootable on the OLPC.  It
        does this by installing a /boot/olpc.fth open firmware configuration
        file that enables booting off of USB and SD cards on the XO.
        """
        from liveusb.olpc import ofw_config
        self.log.info(_('Setting up OLPC boot file...'))
        args = self.get_kernel_args()
        if not os.path.exists(os.path.join(self.dest, 'boot')):
            os.mkdir(os.path.join(self.dest, 'boot'))
        olpc_cfg = file(os.path.join(self.dest, 'boot', 'olpc.fth'), 'w')
        olpc_cfg.write(ofw_config % ' '.join(args))
        olpc_cfg.close()
        self.log.debug('Wrote %s' % olpc_cfg.name)

    def get_kernel_args(self):
        """ Grab the kernel arguments from our syslinux configuration """
        args = []
        cfg = file(os.path.join(self.dest, 'isolinux', 'syslinux.cfg'))
        for line in cfg.readlines():
            if 'append' in line:
                args.extend([arg for arg in line.split()[1:]
                             if not arg.startswith('initrd')])
                break
        cfg.close()
        return args

    def terminate(self):
        """ Terminate any subprocesses that we have spawned """
        raise NotImplementedError

    def mount_device(self):
        """ Mount self.drive, setting the mount point to self.mount """
        raise NotImplementedError

    def unmount_device(self):
        """ Unmount the device mounted at self.mount """
        raise NotImplementedError

    def popen(self, cmd, passive=False, **kwargs):
        """ A wrapper method for running subprocesses.

        This method handles logging of the command and it's output, and keeps
        track of the pids in case we need to kill them.  If something goes
        wrong, an error log is written out and a LiveUSBError is thrown.

        @param cmd: The commandline to execute.  Either a string or a list.
        @param passive: Enable passive process failure.
        @param kwargs: Extra arguments to pass to subprocess.Popen
        """
        self.log.debug(cmd)
        if isinstance(cmd, unicode):
            cmd = cmd.encode(sys.getfilesystemencoding(), 'replace')
        self.output.write(cmd)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.PIPE,
                                shell=True, **kwargs)
        self.pids.append(proc.pid)
        out, err = proc.communicate()
        if isinstance(out, unicode):
            out = out.encode('utf-8', 'replace')
        if isinstance(err, unicode):
            err = err.encode('utf-8', 'replace')
        self.output.write(out + '\n' + err + '\n')
        if proc.returncode:
            filename = self.write_log()
            if not passive:
                raise LiveUSBError(_("There was a problem executing the "
                                     "following command: %r\n%r\nA more detailed "
                                     "error log has been written to "
                                     "'%r'" % (cmd, err, filename)))
        return proc

    def verify_iso_sha1(self, progress=None):
        """ Verify the SHA1 checksum of our ISO if it is in our release list """
        if not progress:
            class DummyProgress:
                def set_max_progress(self, value): pass
                def update_progress(self, value): pass
            progress = DummyProgress()
        release = self.get_release_from_iso()
        if release:
            progress.set_max_progress(self.isosize / 1024)
            if 'sha1' in release:
                self.log.info(_("Verifying SHA1 checksum of LiveCD image..."))
                hash = 'sha1'
                checksum = hashlib.sha1()
            elif 'sha256' in release:
                self.log.info(_("Verifying SHA256 checksum of LiveCD image..."))
                hash = 'sha256'
                checksum = hashlib.sha256()
            isofile = file(self.iso, 'rb')
            bytes = 1024**2
            total = 0
            while bytes:
                data = isofile.read(bytes)
                checksum.update(data)
                bytes = len(data)
                total += bytes
                progress.update_progress(total / 1024)
            isofile.close()
            if checksum.hexdigest() == release[hash]:
                return True
            else:
                self.log.info(_("Error: The SHA1 of your Live CD is "
                                "invalid.  You can run this program with "
                                "the --noverify argument to bypass this "
                                "verification check."))
                return False
        else:
            self.log.debug(_('Unknown ISO, skipping checksum verification'))

    def check_free_space(self):
        """ Make sure there is enough space for the LiveOS and overlay """
        freebytes = self.get_free_bytes()
        self.log.debug('freebytes = %d' % freebytes)
        self.log.debug('isosize = %d' % self.isosize)
        overlaysize = self.overlay * 1024**2
        self.log.debug('overlaysize = %d' % overlaysize)
        self.totalsize = overlaysize + self.isosize
        if self.totalsize > freebytes:
            raise LiveUSBError(_("There is not enough free space on the selected device.\nRequired: %s. Free: %s." %
                                 (str(self.isosize/1024**2 + self.overlay) + "MB",
                                  str(freebytes/1024**2) + "MB")))

    def create_persistent_overlay(self):
        if self.overlay:
            self.log.info(_("Creating") + " %sMB " % self.overlay +
                          _("persistent overlay"))
            if self.fstype == 'vfat':
                # vfat apparently can't handle sparse files
                self.popen('dd if=/dev/zero of="%s" count=%d bs=1M'
                           % (self.get_overlay(), self.overlay))
            else:
                self.popen('dd if=/dev/zero of="%s" count=1 bs=1M seek=%d'
                           % (self.get_overlay(), self.overlay))

    def _update_configs(self, infile, outfile):
        infile = file(infile, 'r')
        outfile= file(outfile, 'w')
        usblabel = 'LABEL=' + self.label
        for line in infile.readlines():
            if "LABEL" in line:
                line = re.sub("LABEL=[^ :]*", usblabel, line)
                line = re.sub("rootfstype=[^ ]*",
                              "rootfstype=%s" % self.fstype,
                              line)
            if "isolinux" in line:
                line = re.sub("isolinux", "syslinux", line)
            if "liveimg" in line:
                if self.overlay:
                    line = line.replace("liveimg", "liveimg overlay=" + usblabel)
                    line = line.replace(" ro ", " rw ")
                if self.opts.kernel_args:
                    line = line.replace("liveimg", "liveimg %s" %
                                        ' '.join(self.opts.kernel_args.split(',')))
            elif "rd.live.image" in line:
                if self.overlay:
                    line = line.replace("rd.live.image", "rd.live.image " +
                                        "rd.live.overlay=" + usblabel)
                    line = line.replace(" ro ", " rw ")
                if self.opts.kernel_args:
                    line = line.replace("rd.live.image", "rd.live.image %s" %
                                        ' '.join(self.opts.kernel_args.split(',')))
            outfile.write(line)
        infile.close()
        outfile.close()

    def update_configs(self):
        """ Generate our syslinux.cfg and grub.conf files """
        grubconf     = os.path.join(self.dest, "EFI", "BOOT", "grub.cfg")
        bootconf     = os.path.join(self.dest, "EFI", "BOOT", "boot.conf")
        bootx64conf  = os.path.join(self.dest, "EFI", "BOOT", "bootx64.conf")
        bootia32conf = os.path.join(self.dest, "EFI", "BOOT", "bootia32.conf")
        updates = [(os.path.join(self.dest, "isolinux", "isolinux.cfg"),
                    os.path.join(self.dest, "isolinux", "syslinux.cfg")),
                   (grubconf, bootconf)]
        copies = [(bootconf, grubconf),
                  (bootconf, bootx64conf),
                  (bootconf, bootia32conf)]

        for (infile, outfile) in updates:
            if os.path.exists(infile):
                self._update_configs(infile, outfile)
        # only copy/overwrite files we had originally started with
        for (infile, outfile) in copies:
            if os.path.exists(outfile):
                try:
                    shutil.copyfile(infile, outfile)
                except Exception, e:
                    self.log.warning(_("Unable to copy %r to %r: %r") % (infile, outfile, e))

    def delete_ldlinux(self):
        # Don't prompt about overwriting files from mtools (#491234)
        for ldlinux in [os.path.join(self.dest, p, 'ldlinux.sys')
                        for p in ('syslinux', '')]:
            self.log.debug('Looking for %s' % ldlinux)
            if os.path.isfile(ldlinux):
                self.log.debug(_("Removing") + " %s" % ldlinux)
                self.popen('chattr -i "%s"' % ldlinux, passive=True)
                os.unlink(ldlinux)

    def delete_liveos(self):
        """ Delete the existing LiveOS """
        self.log.info(_('Removing existing Live OS'))
        self.delete_ldlinux()
        for path in [self.get_liveos(),
                     os.path.join(self.dest + os.path.sep, 'syslinux'),
                     os.path.join(self.dest + os.path.sep, 'isolinux')]:
            if os.path.exists(path):
                self.log.debug(_("Deleting ") + path)
                # Python for Windows is unable to delete read-only files,
                if os.path.isdir(path):
                    for f in os.listdir(path):
                        try:
                            os.chmod(os.path.join(path, f), 0777)
                        except OSError, e:
                            self.log.debug(_("Unable to delete %r: %r") % (f, e))
                try:
                    shutil.rmtree(path)
                except OSError, e:
                    raise LiveUSBError(_("Unable to remove previous LiveOS: "
                                         "%r" % e))

    def write_log(self):
        """ Write out our subprocess stdout/stderr to a log file """
        tmpdir = os.getenv('TEMP', '/tmp')
        filename = os.path.join(tmpdir, 'liveusb-creator.log')
        out = file(filename, 'a')
        out.write(self.output.getvalue())
        out.close()
        return filename


    def existing_liveos(self):
        return os.path.exists(self.get_liveos())

    def get_liveos(self):
        if not os.path.exists(self.dest):
            raise LiveUSBError(_('Cannot find path %r') % self.dest)
        return os.path.join(self.dest + os.path.sep, "LiveOS")

    def existing_overlay(self):
        return os.path.exists(self.get_overlay())

    def get_overlay(self):
        return os.path.join(self.get_liveos(),
                            'overlay-%s-%s' % (self.label, self.uuid or ''))

    def get_release_from_iso(self):
        """ If the ISO is for a known release, return it. """
        isoname = os.path.basename(self.iso)
        for release in releases:
            for arch in release['variants'].keys():
                if arch in release['variants'].keys() and 'url' in release['variants'][arch] and os.path.basename(release['variants'][arch]['url']) == isoname:
                    return release
        return None

    def _set_drive(self, drive):
        if drive == None:
            self._drive = None
            return
        if not self.drives.has_key(drive):
            found = False
            for key in self.drives.keys():
                if self.drives[key]['device'] == drive:
                    drive = key
                    found = True
                    break
            if not found:
                raise LiveUSBError(_("Cannot find device %s" % drive))
        self.log.debug("%s selected: %s" % (drive, self.drives[drive]))
        self._drive = drive
        self.uuid = self.drives[drive]['uuid']
        self.fstype = self.drives[drive]['fstype']

    def get_proxies(self):
        """ Return a dictionary of proxy settings """
        return None

    def bootable_partition(self):
        """ Ensure that the selected partition is flagged as bootable """
        pass

    def set_iso(self, iso):
        """ Select the given ISO """
        self.iso = os.path.abspath(self._to_unicode(iso))
        self.isosize = os.stat(self.iso)[ST_SIZE]

    def _to_unicode(self, obj, encoding='utf-8'):
        if hasattr(obj, 'toUtf8'): # PyQt5.QtCore.QString
            obj = str(obj.toUtf8())
        if isinstance(obj, basestring):
            if not isinstance(obj, unicode):
                obj = unicode(obj, encoding, 'replace')
        return obj

    def get_mbr(self):
        pass

    def blank_mbr(self):
        pass

    def mbr_matches_syslinux_bin(self):
        """
        Return whether or not the MBR on the drive matches the system's
        syslinux mbr.bin
        """
        return True

    def reset_mbr(self):
        pass

    def flush_buffers(self):
        """ Flush filesystem buffers """
        pass

    def is_admin(self):
        raise NotImplementedError

    def dd_image(self):
        self.log.info(_('Overwriting device with live image'))
        parent = self.drive['parent']
        if parent:
            drive = parent
        else:
            drive = self.drive['device']
        cmd = 'dd if="%s" of="%s" bs=1M iflag=direct oflag=direct conv=fdatasync' % (self.iso, drive)
        self.log.debug(_('Running') + ' %s' % cmd)
        self.popen(cmd)

    def calculate_liveos_checksum(self):
        """ Calculate the hash of the extracted LiveOS """
        chunk_size = 1024 # FIXME: optimize this.  we hit bugs when this is *not* 1024
        checksums = []
        mnt = self.drive['mount']
        if not os.path.isdir(mnt):
            dev = self.drive['device']
            if os.path.isdir(dev):
                mnt = dev  # on Windows
            else:
                self.log.error('Cannot find mount point for %s', self.drive)
        for img in (os.path.join('LiveOS', 'osmin.img'),
                    os.path.join('LiveOS', 'squashfs.img'),
                    os.path.join('syslinux', 'initrd0.img'),
                    os.path.join('syslinux', 'vmlinuz0'),
                    os.path.join('syslinux', 'isolinux.bin')):
            hash = getattr(hashlib, self.opts.hash, 'sha1')()
            liveos = os.path.join(mnt, img)
            device = file(liveos, 'rb')
            self.log.info("Calculating the %s of %s" % (hash.name, liveos))
            bytes = 1
            while bytes:
                data = device.read(chunk_size)
                hash.update(data)
                bytes = len(data)
            checksum = hash.hexdigest()
            checksums.append(checksum)
            self.log.info('%s(%s) = %s' % (hash.name, liveos, checksum))

        # Take a checksum of all of the checksums
        hash = getattr(hashlib, self.opts.hash, 'sha1')()
        map(hash.update, checksums)
        self.log.info("%s = %s" % (hash.name, hash.hexdigest()))


class LinuxLiveUSBCreator(LiveUSBCreator):

    bus = None # the dbus.SystemBus
    udisks = None # the org.freedesktop.UDisks2 dbus.Interface

    def __init__(self, *args, **kw):
        super(LinuxLiveUSBCreator, self).__init__(*args, **kw)
        extlinux = self.get_extlinux_version()
        if extlinux is None:
            self.valid_fstypes -= self.ext_fstypes
        elif extlinux < 4:
            self.log.debug(_('You are using an old version of syslinux-extlinux '
                    'that does not support the ext4 filesystem'))
            self.valid_fstypes -= set(['ext4'])

    def strify(self, s):
        return bytearray(s).replace(b'\x00', b'').decode('utf-8')

    def detect_removable_drives(self, callback=None):
        """ Detect all removable USB storage devices using UDisks2 via D-Bus """
        import dbus
        self.callback = callback
        self.drives = {}
        self.bus = dbus.SystemBus()
        """
        udisks_obj = self.bus.get_object("org.freedesktop.UDisks",
                                         "/org/freedesktop/UDisks")
        self.udisks = dbus.Interface(udisks_obj, "org.freedesktop.UDisks")

        def handle_reply(devices):
            for device in devices:
                dev_obj = self.bus.get_object("org.freedesktop.UDisks", device)
                dev = dbus.Interface(dev_obj, "org.freedesktop.DBus.Properties")

                data = {
                    'udi': str(device),
                    'is_optical': bool(dev.Get(device, 'DeviceIsOpticalDisc')),
                    'label': unicode(dev.Get(device, 'IdLabel')).replace(' ', '_'),
                    'fstype': str(dev.Get(device, 'IdType')),
                    'fsversion': str(dev.Get(device, 'IdVersion')),
                    'uuid': str(dev.Get(device, 'IdUuid')),
                    'device': str(dev.Get(device, 'DeviceFile')),
                    'mount': map(unicode, list(dev.Get(device, 'DeviceMountPaths'))),
                    'bootable': 'boot' in map(str,
                        list(dev.Get(device, 'PartitionFlags'))),
                    'parent': None,
                    'size': int(dev.Get(device, 'DeviceSize')),
                    'fullSize': int(dev.Get(device, 'DeviceSize')),
                    'model': str(dev.Get(device, 'DriveModel')),
                    'vendor': str(dev.Get(device, 'DriveVendor'))
                }

                # Only pay attention to USB devices, unless --force'd
                iface = str(dev.Get(device, 'DriveConnectionInterface'))
                if iface != 'usb' and self.opts.force != data['device']:
                    self.log.warning('Skipping non-usb drive: %s' % device)
                    continue

                # Skip optical drives
                if data['is_optical'] and self.opts.force != data['device']:
                    self.log.debug('Skipping optical device: %s' % data['device'])
                    continue

                # Skip things without a size
                if not data['size'] and not self.opts.force:
                    self.log.debug('Skipping device without size: %s' % device)
                    continue

                # Skip devices with unknown filesystems
                if data['fstype'] not in self.valid_fstypes and \
                        self.opts.force != data['device']:
                    self.log.debug('Skipping %s with unknown filesystem: %s' % (
                        data['device'], data['fstype']))
                    continue

                parent = dev.Get(device, 'PartitionSlave')
                if parent and parent != '/':
                    data['parent'] = str(dbus.Interface(self._get_device(parent),
                            'org.freedesktop.DBus.Properties').Get(parent,
                                'DeviceFile'))
                    data['fullSize'] = int(dbus.Interface(self._get_device(parent),
                            'org.freedesktop.DBus.Properties').Get(parent,
                                'DeviceSize'))

                mount = data['mount']
                if mount:
                    if len(mount) > 1:
                        self.log.warning('Multiple mount points for %s' %
                                data['device'])
                    mount = data['mount'] = data['mount'][0]
                else:
                    mount = data['mount'] = None
        """
        udisks_obj = self.bus.get_object("org.freedesktop.UDisks2",
                                         "/org/freedesktop/UDisks2")
        self.udisks = dbus.Interface(udisks_obj, 'org.freedesktop.DBus.ObjectManager')


        def handleAdded(name, device):
            if ('org.freedesktop.UDisks2.Block' in device and
                'org.freedesktop.UDisks2.Filesystem' in device and
                'org.freedesktop.UDisks2.Partition' in device):
                self.log.debug('Found block device with filesystem on %s' % name)
            else:
                return

            partition = device['org.freedesktop.UDisks2.Partition']
            fs = device['org.freedesktop.UDisks2.Filesystem']
            blk = device['org.freedesktop.UDisks2.Block']

            if blk['Drive'] == '/':
                self.log.debug('Skipping root drive: %s' % name)
                return

            drive_obj = self.bus.get_object("org.freedesktop.UDisks2", blk['Drive'])
            drive = dbus.Interface(drive_obj, "org.freedesktop.DBus.Properties").GetAll("org.freedesktop.UDisks2.Drive")

            # this is probably the only check we need, including Drive != "/"
            if (not drive[u'Removable'] or
                drive[u'Optical'] or
                    (drive[u'ConnectionBus'] != 'usb' and
                     drive[u'ConnectionBus'] != 'sdio')):
                self.log.debug('Skipping a device that is not removable, connected via USB or is optical: %s' % name)
                return

            data = {
                'udi': str(blk['Drive']),
                'label': str(blk['IdLabel']),
                'fstype': str(blk['IdType']),
                'fsversion': str(blk['IdVersion']),
                'uuid': str(blk['IdUUID']),
                'device': self.strify(blk['Device']),
                'mount': map(self.strify, fs['MountPoints']),
                'size': int(blk['Size']),
                'friendlyName': str(drive['Vendor']) + ' ' + str(drive['Model'])
            }
            self.log.debug('data = %r' % data)

            if '/boot' in data['mount']:
                self.log.debug('Skipping boot device: %s' % name)
                return

            # Skip things without a size
            if not data['size'] and not self.opts.force:
                self.log.debug('Skipping device without size: %s' % device)
                return

            # Skip devices with unknown filesystems
            if data['fstype'] not in self.valid_fstypes and \
                    self.opts.force != data['device']:
                self.log.debug('Skipping %s with unknown filesystem: %s' % (
                    data['device'], data['fstype']))
                return

            mount = data['mount']
            if mount:
                if len(mount) > 1:
                    self.log.warning('Multiple mount points for %s' %
                            data['device'])
                mount = data['mount'] = data['mount'][0]
            else:
                mount = data['mount'] = None

            data['free'] = mount and \
                    self.get_free_bytes(mount) / 1024**2 or None


            parent_obj = self.bus.get_object("org.freedesktop.UDisks2", partition[u'Table'])
            parent = dbus.Interface(parent_obj, "org.freedesktop.DBus.Properties").Get("org.freedesktop.UDisks2.Block", "Device")
            data['parent'] = self.strify(parent)

            self.log.debug(pformat(data))

            self.drives[name] = data

            if self.callback:
                self.callback()

        def handleRemoved(path, interfaces):
            if self.drives.has_key(path):
                del self.drives[path]

            if self.callback:
                self.callback()

        self.bus.add_signal_receiver(handleAdded, "InterfacesAdded", "org.freedesktop.DBus.ObjectManager", "org.freedesktop.UDisks2", "/org/freedesktop/UDisks2")
        self.bus.add_signal_receiver(handleRemoved, "InterfacesRemoved", "org.freedesktop.DBus.ObjectManager", "org.freedesktop.UDisks2", "/org/freedesktop/UDisks2")

        for name, device in self.udisks.GetManagedObjects().iteritems():
            handleAdded(name, device)

    def _storage_bus(self, dev):
        storage_bus = None
        try:
            storage_bus = dev.GetProperty('storage.bus')
        except Exception, e:
            self.log.exception(e)
        return storage_bus

    def _block_is_volume(self, dev):
        is_volume = False
        try:
            is_volume = dev.GetProperty("block.is_volume")
        except Exception, e:
            self.log.exception(e)
        return is_volume

    def _add_device(self, dev, parent=None):
        mount = unicode(dev.GetProperty('volume.mount_point'))
        device = str(dev.GetProperty('block.device'))
        if parent:
            parent = parent.GetProperty('block.device')
        self.drives[device] = {
            'label'   : unicode(dev.GetProperty('volume.label')).replace(' ', '_'),
            'fstype'  : str(dev.GetProperty('volume.fstype')),
            'fsversion': str(dev.GetProperty('volume.fsversion')),
            'uuid'    : str(dev.GetProperty('volume.uuid')),
            'mount'   : mount,
            'udi'     : dev,
            'free'    : mount and self.get_free_bytes(mount) / 1024**2 or None,
            'device'  : device,
            'parent'  : parent
        }

    def mount_device(self):
        """ Mount our device if it is not already mounted """
        import dbus
        if not self.fstype:
            raise LiveUSBError(_("Unknown filesystem.  Your device "
                                 "may need to be reformatted."))
        if self.fstype not in self.valid_fstypes:
            raise LiveUSBError(_("Unsupported filesystem: %s") %
                                 self.fstype)
        self.dest = self.drive['mount']
        mnt = None
        if not self.dest:
            dev=None
            bd=None
            try:
                dev = self._get_device_fs(self.drive['udi'])
                bd = self.bus.get_object('org.freedesktop.UDisks2',
                                   '/org/freedesktop/UDisks2/block_devices%s' %
                                   self.drive['device'][4:])
            except dbus.exceptions.DBusException, e:
                self.log.error(_('Unknown dbus exception while trying to '
                                     'mount device: %s') % str(e))

            if dev and bd:
                try:
                    mntpnts = dbus.Interface(bd, 'org.freedesktop.DBus.Properties').Get('org.freedesktop.UDisks2.Filesystem', 'MountPoints')
                    if len(mntpnts) > 0:
                        self.log.debug("%s is already mounted at %s" % (self.drive['device'], self.strify(mntpnts[0])))
                        mnt = self.strify(mntpnts[0])
                except dbus.exceptions.DBusException, e:
                    pass

            if dev and bd and not mnt:
                try:
                    self.log.debug("Mounting %s" % self.drive['device'])
                    mnt = str(bd.Mount({}, dbus_interface='org.freedesktop.UDisks2.Filesystem'))
                except dbus.exceptions.DBusException, e:
                    self.log.error(_('Unknown dbus exception while trying to '
                                     'mount device: %s') % str(e))
                except Exception, e:
                    raise LiveUSBError(_("Unable to mount device: %r" % e))

            if mnt and not os.path.exists(mnt):
                self.log.error(_('No mount points found after mounting attempt'))
                self.log.error("%s doesn't exist" % mnt)
            else:
                self.dest = self.drive['mount'] = mnt
                self.drive['free'] = self.get_free_bytes(self.dest) / 1024**2
                self.log.debug("Mounted %s to %s " % (self.drive['device'],
                                                      self.dest))
        else:
            self.log.debug(_("Using existing mount: %s") % self.dest)

    def unmount_device(self):
        """ Unmount our device """
        self.log.info("Unmounting %s" % self.dest)
        self.popen('umount %s' % self.drive['device'], passive=True)
        self.drive['mount'] = None
        if os.path.exists(self.dest):
            self.log.error("Mount %s exists after unmounting" % self.dest)
        self.dest = None

    def verify_filesystem(self):
        self.log.info(_("Verifying filesystem..."))
        if self.fstype not in self.valid_fstypes:
            if not self.fstype:
                raise LiveUSBError(_("Unknown filesystem.  Your device "
                                     "may need to be reformatted."))
            else:
                raise LiveUSBError(_("Unsupported filesystem: %s" %
                                     self.fstype))
        if self.drive['label'] != self.label:
            self.log.info(_("Setting %s label to %s") % (self.drive['device'],
                                                         self.label))
            try:
                if self.fstype in ('vfat', 'msdos'):
                    try:
                        self.popen('/sbin/dosfslabel %s %s' % (
                                   self.drive['device'], self.label))
                    except LiveUSBError:
                        # dosfslabel returns an error code even upon success
                        pass
                else:
                    self.popen('/sbin/e2label %s %s' % (self.drive['device'],
                                                        self.label))
            except LiveUSBError, e:
                self.log.error(_("Unable to change volume label: %r") % e)

    def extract_iso(self):
        """ Extract self.iso to self.dest """
        self.log.info(_("Extracting live image to USB device..."))
        tmpdir = tempfile.mkdtemp()
        self.popen('mount -o loop,ro "%s" %s' % (self.iso, tmpdir))
        tmpliveos = os.path.join(tmpdir, 'LiveOS')
        try:
            if not os.path.isdir(tmpliveos):
                raise LiveUSBError(_("Unable to find LiveOS on ISO"))
            liveos = os.path.join(self.dest, 'LiveOS')
            if not os.path.exists(liveos):
                os.mkdir(liveos)

            start = datetime.now()
            self.popen("cp %s '%s'" % (os.path.join(tmpliveos, 'squashfs.img'),
                                       os.path.join(liveos, 'squashfs.img')))
            delta = datetime.now() - start
            if delta.seconds:
                self.mb_per_sec = (self.isosize / delta.seconds) / 1024**2
                if self.mb_per_sec:
                    self.log.info(_("Wrote to device at") + " %d MB/sec" %
                                  self.mb_per_sec)

            osmin = os.path.join(tmpliveos, 'osmin.img')
            if os.path.exists(osmin):
                self.popen("cp %s '%s'" % (osmin,
                    os.path.join(liveos, 'osmin.img')))
            else:
                self.log.debug('No osmin.img found')

            isolinux = os.path.join(self.dest, 'isolinux')
            if not os.path.exists(isolinux):
                os.mkdir(isolinux)
            self.popen("cp %s/* '%s'" % (
                os.path.join(tmpdir, 'isolinux'), isolinux))

            if os.path.exists(os.path.join(tmpdir, 'EFI')):
                efi = os.path.join(self.dest, 'EFI')
                if not os.path.exists(efi):
                    os.mkdir(efi)
                    self.popen("cp -r %s/* '%s'" % (os.path.join(tmpdir, 'EFI'),
                                                    efi))
        finally:
            self.popen('umount %s' % tmpdir)

    def install_bootloader(self):
        """ Run syslinux to install the bootloader on our devices """
        LiveUSBCreator.install_bootloader(self)
        self.log.info(_("Installing bootloader..."))
        syslinux_path = os.path.join(self.dest, "syslinux")
        try:
            shutil.rmtree(syslinux_path)
        except OSError, e:
            pass
        print (self.dest, syslinux_path)
        shutil.move(os.path.join(self.dest, "isolinux"), syslinux_path)
        try:
            os.unlink(os.path.join(syslinux_path, "isolinux.cfg"))
        except OSError, e:
            pass

        # Syslinux doesn't guarantee the API for its com32 modules (#492370)
        for com32mod in ('vesamenu.c32', 'menu.c32'):
            copied = False
            for path in ('/usr/share', '/usr/lib'):
                com32path = os.path.join(path, 'syslinux', com32mod)
                if os.path.isfile(com32path):
                    self.log.debug('Copying %s on to stick' % com32path)
                    shutil.copyfile(com32path, os.path.join(syslinux_path, com32mod))
                    copied = True
                    break
            if copied:
                break

        self.delete_ldlinux()

        if self.drive['fstype'] in self.ext_fstypes:
            shutil.move(os.path.join(syslinux_path, "syslinux.cfg"),
                        os.path.join(syslinux_path, "extlinux.conf"))
            self.popen("extlinux -i '%s'" % syslinux_path)
        else: # FAT
            self.popen('syslinux%s%s -d %s %s' %  (self.opts.force and
                       ' -f' or '', self.opts.safe and ' -s' or '',
                       'syslinux', self.drive['device']))

    def get_free_bytes(self, device=None):
        """ Return the number of available bytes on our device """
        import statvfs
        device = device and device or self.dest
        if not device:
            return 0
        device = device.encode('utf-8')
        if not os.path.exists(device):
            raise LiveUSBError(_('Cannot find device: %s') % device)
        stat = os.statvfs(device)
        return stat[statvfs.F_BSIZE] * stat[statvfs.F_BAVAIL]

    def _get_device_fs(self, udi):
        """ Return a dbus Interface to a specific UDisks device UDI """
        import dbus
        dev_obj = self.bus.get_object("org.freedesktop.UDisks2", udi)
        return dbus.Interface(dev_obj, "org.freedesktop.UDisks2.Filesystem")

    def terminate(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGHUP)
                self.log.debug("Killed process %d" % pid)
            except OSError, e:
                self.log.debug(repr(e))

    def verify_iso_md5(self):
        """ Verify the ISO md5sum.

        At the moment this is Linux specific, until we port checkisomd5
        to Windows.
        """
        self.log.info(_('Verifying ISO MD5 checksum'))
        try:
            self.popen('checkisomd5 "%s"' % self.iso)
        except LiveUSBError, e:
            self.log.exception(e)
            self.log.info(_('ISO MD5 checksum verification failed'))
            return False
        self.log.info(_('ISO MD5 checksum passed'))
        return True

    def get_proxies(self):
        """ Return the proxy settings.

        At the moment this implementation only works on KDE, and should
        eventually be expanded to support other platforms as well.
        """
        try:
            from PyQt5 import QtCore
        except ImportError:
            self.log.warning("PyQt5 module not installed; skipping KDE "
                             "proxy detection")
            return
        kioslaverc = QtCore.QDir.homePath() + '/.kde/share/config/kioslaverc'
        if not QtCore.QFile.exists(kioslaverc):
            return {}
        settings = QtCore.QSettings(kioslaverc, QtCore.QSettings.IniFormat)
        settings.beginGroup('Proxy Settings')
        proxies = {}
        # check for KProtocolManager::ManualProxy (the only one we support)
        if settings.value('ProxyType') and settings.value('ProxyType').toInt()[0] == 1:
            httpProxy = settings.value('httpProxy').toString()
            if httpProxy != '':
                proxies['http'] = httpProxy
            ftpProxy = settings.value('ftpProxy').toString()
            if ftpProxy != '':
                proxies['ftp'] = ftpProxy
        return proxies

    def bootable_partition(self):
        """ Ensure that the selected partition is flagged as bootable """
        if self.drive.get('parent') is None:
            self.log.debug('No partitions on device; not attempting to mark '
                           'any paritions as bootable')
            return
        import parted
        try:
            disk, partition = self.get_disk_partition()
        except LiveUSBError:
            self.log.exception(_('Unable to get disk partitions'))
            return
        if partition.isFlagAvailable(parted.PARTITION_BOOT):
            if partition.getFlag(parted.PARTITION_BOOT):
                self.log.debug(_('%s already bootable') % self.drive['device'])
            else:
                partition.setFlag(parted.PARTITION_BOOT)
                try:
                    disk.commit()
                    self.log.info('Marked %s as bootable' % self.drive['device'])
                except Exception, e:
                    self.log.exception(e)
        else:
            self.log.warning('%s does not have boot flag' % self.drive['device'])

    def get_disk_partition(self):
        """ Return the PedDisk and partition of the selected device """
        import parted
        parent = self.drives[self._drive]['parent']
        dev = parted.Device(path = parent)
        disk = parted.Disk(device = dev)
        for part in disk.partitions:
            if self.drive['device'] == "/dev/%s" %(part.getDeviceNodeName(),):
                return disk, part
        raise LiveUSBError(_("Unable to find partition"))

    def initialize_zip_geometry(self):
        """ This method initializes the selected device in a zip-like fashion.

        :Note: This feature is currently experimental, and will DESTROY ALL DATA
               on your device!

        More details on this can be found here:
            http://syslinux.zytor.com/doc/usbkey.txt
        """
        #from parted import PedDevice
        self.log.info('Initializing %s in a zip-like fashon' % self.drive['device'])
        heads = 64
        cylinders = 32
        # Is this part even necessary?
        #device = PedDevice.get(self._drive[:-1])
        #cylinders = int(device.cylinders / (64 * 32))
        self.popen('/usr/lib/syslinux/mkdiskimage -4 %s 0 %d %d' % (
                   self.drive['device'][:-1], heads, cylinders))

    def format_device(self):
        """ Format the selected partition as FAT32 """
        self.log.info('Formatting %s as FAT32' % self.drive['device'])
        self.popen('mkfs.vfat -F 32 %s' % self.drive['device'])

    def get_mbr(self):
        parent = self.drive.get('parent', self.drive['device'])
        if parent is None:
            parent = self.drive['device']
        parent = unicode(parent)
        self.log.debug('Checking the MBR of %s' % parent)
        try:
            drive = open(parent, 'rb')
        except IOError:
            return ''
        mbr = ''.join(['%02X' % ord(x) for x in drive.read(2)])
        drive.close()
        self.log.debug('mbr = %r' % mbr)
        return mbr

    def blank_mbr(self):
        """ Return whether the MBR is empty or not """
        return self.get_mbr() == '0000'

    def _get_mbr_bin(self):
        mbr = None
        for mbr_bin in ('/usr/lib/syslinux/mbr.bin',
                        '/usr/share/syslinux/mbr.bin',
                        '/usr/lib/syslinux/bios/mbr.bin',
                        '/usr/lib/syslinux/mbr/mbr.bin'):
            if os.path.exists(mbr_bin):
                mbr = mbr_bin
        return mbr

    def mbr_matches_syslinux_bin(self):
        """
        Return whether or not the MBR on the drive matches the system's
        syslinux mbr.bin
        """
        mbr_bin = open(self._get_mbr_bin(), 'rb')
        mbr = ''.join(['%02X' % ord(x) for x in mbr_bin.read(2)])
        return mbr == self.get_mbr()

    def reset_mbr(self):
        parent = unicode(self.drive.get('parent', self.drive['device']))
        if '/dev/loop' not in self.drive:
            mbr = self._get_mbr_bin()
            if mbr:
                self.log.info(_('Resetting Master Boot Record') + ' of %s' % parent)
                self.popen('cat %s > %s' % (mbr, parent))
            else:
                self.log.info(_('Unable to reset MBR.  You may not have the '
                                '`syslinux` package installed'))
        else:
            self.log.info(_('Drive is a loopback, skipping MBR reset'))

    def calculate_device_checksum(self, progress=None):
        """ Calculate the SHA1 checksum of the device """
        self.log.info(_("Calculating the SHA1 of %s" % self.drive['device']))
        if not progress:
            class DummyProgress:
                def set_max_progress(self, value): pass
                def update_progress(self, value): pass
            progress = DummyProgress()
        # Get size of drive
        #progress.set_max_progress(self.isosize / 1024)
        checksum = hashlib.sha1()
        device_name = unicode(self.drive['parent'])
        device = file(device_name, 'rb')
        bytes = 1024**2
        total = 0
        while bytes:
            data = device.read(bytes)
            checksum.update(data)
            bytes = len(data)
            total += bytes
            progress.update_progress(total / 1024)
        hexdigest = checksum.hexdigest()
        self.log.info("sha1(%s) = %s" % (device_name, hexdigest))
        return hexdigest

    def flush_buffers(self):
        self.popen('sync', passive=True)

    def is_admin(self):
        return os.getuid() == 0

    def get_extlinux_version(self):
        """ Return the version of extlinux. None if it isn't installed """
        import subprocess
        version = None
        p = subprocess.Popen('extlinux -v', shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        if p.returncode == 0:
            version = int(err.split()[1].split('.')[0])
        elif p.returncode == 127:
            self.log.warning('extlinux not found! Only FAT filesystems will be supported')
        else:
            self.log.debug('Unknown return code from extlinux: %s' % p.returncode)
            self.log.debug('stdout: %r\nstderr: %r' % (out, err))
        return version



class MacOsLiveUSBCreator(object):

    def detect_removable_drives(self, callback=None):
        """ This method should populate self.drives with removable devices """
        pass

    def verify_filesystem(self):
        """
        Verify the filesystem of our device, setting the volume label
        if necessary.  If something is not right, this method throws a
        LiveUSBError.
        """
        pass

    def get_free_bytes(self, drive=None):
        """ Return the number of free bytes on a given drive.

        If drive is None, then use the currently selected device.
        """
        pass

    def extract_iso(self):
        """ Extract the LiveCD ISO to the USB drive """
        pass

    def verify_iso_md5(self):
        """ Verify the MD5 checksum of the ISO """
        pass

    def terminate(self):
        """ Terminate any subprocesses that we have spawned """
        pass

    def mount_device(self):
        """ Mount self.drive, setting the mount point to self.mount """
        pass

    def unmount_device(self):
        """ Unmount the device mounted at self.mount """
        pass

    def get_proxies(self):
        """ Return a dictionary of proxy settings """
        pass

    def bootable_partition(self):
        """ Ensure that the selected partition is flagged as bootable """
        pass

    def get_mbr(self):
        pass

    def blank_mbr(self):
        pass

    def reset_mbr(self):
        pass

    def flush_buffers(self):
        """ Flush filesystem buffers """
        pass

    def is_admin(self):
        return os.getuid() == 0


class WindowsLiveUSBCreator(LiveUSBCreator):

    def detect_removable_drives(self, callback=None):
        import win32file, win32api, pywintypes
        self.drives = {}
        for drive in [l + ':' for l in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ']:
            try:
                if win32file.GetDriveType(drive) == win32file.DRIVE_REMOVABLE or \
                   drive == self.opts.force:
                    vol = [None]
                    try:
                        vol = win32api.GetVolumeInformation(drive)
                    except pywintypes.error, e:
                        self.log.error(_('Unable to get GetVolumeInformation(%r): %r') % (drive, e))
                        continue
                    self.drives[drive] = {
                        'label': vol[0],
                        'mount': drive,
                        'uuid': self._get_device_uuid(drive),
                        'free': self.get_free_bytes(drive) / 1024**2,
                        'fstype': 'vfat',
                        'device': drive,
                        'fsversion': vol[-1],
                        'size': self._get_device_size(drive)
                    }
            except Exception, e:
                self.log.exception(e)
                self.log.error(_("Error probing device"))
        if callback:
            callback()

    def verify_filesystem(self):
        import win32api, win32file, pywintypes
        self.log.info(_("Verifying filesystem..."))
        try:
            vol = win32api.GetVolumeInformation(self.drive['device'])
        except Exception, e:
            raise LiveUSBError(_("Make sure your USB key is plugged in and "
                                 "formatted with the FAT filesystem"))
        if vol[-1] not in ('FAT32', 'FAT'):
            raise LiveUSBError(_("Unsupported filesystem: %s\nPlease backup "
                                 "and format your USB key with the FAT "
                                 "filesystem." % vol[-1]))
        self.fstype = 'vfat'
        if vol[0] != self.label:
            try:
                win32file.SetVolumeLabel(self.drive['device'], self.label)
                self.log.debug("Set %s label to %s" % (self.drive['device'],
                                                       self.label))
            except pywintypes.error, e:
                self.log.warning(_("Unable to SetVolumeLabel: %r") % e)

    def get_free_bytes(self, device=None):
        """ Return the number of free bytes on our selected drive """
        import win32file
        device = device and device or self.drive['device']
        try:
            (spc, bps, fc, tc) = win32file.GetDiskFreeSpace(device)
        except Exception, e:
            self.log.error(_("Problem determining free space: %r") % e)
            return 0
        return fc * (spc * bps) # free-clusters * bytes per-cluster

    def extract_iso(self):
        """ Extract our ISO with 7-zip directly to the USB key """
        self.log.info(_("Extracting live image to USB device..."))
        start = datetime.now()
        self.popen('7z x "%s" -x![BOOT] -y -o%s' % (
                   self.iso, self.drive['device']))
        delta = datetime.now() - start
        if delta.seconds:
            self.mb_per_sec = (self.isosize / delta.seconds) / 1024**2
            if self.mb_per_sec:
                self.log.info(_("Wrote to device at") + " %d MB/sec" %
                              self.mb_per_sec)

    def install_bootloader(self):
        """ Run syslinux to install the bootloader on our device """
        LiveUSBCreator.install_bootloader(self)
        self.log.info(_("Installing bootloader"))
        device = self.drive['device']
        syslinuxdir = os.path.join(device + os.path.sep, "syslinux")
        if os.path.isdir(syslinuxdir):
            # Python for Windows is unable to delete read-only files, and some
            # may exist here if the LiveUSB stick was created in Linux
            for f in os.listdir(syslinuxdir):
                os.chmod(os.path.join(syslinuxdir, f), 0777)
            shutil.rmtree(syslinuxdir)
        shutil.move(os.path.join(device + os.path.sep, "isolinux"), syslinuxdir)
        os.unlink(os.path.join(syslinuxdir, "isolinux.cfg"))

        # Don't prompt about overwriting files from mtools (#491234)
        for ldlinux in [os.path.join(device + os.path.sep, p, 'ldlinux.sys')
                        for p in (syslinuxdir, '')]:
            if os.path.isfile(ldlinux):
                os.chmod(ldlinux, 0777)
                self.log.debug(_("Removing") + " %s" % ldlinux)
                os.unlink(ldlinux)

        self.popen('syslinux%s%s -m -a -d %s %s' %  (self.opts.force and ' -f'
                   or '', self.opts.safe and ' -s' or '', 'syslinux', device))

    # Cache these, because they are fairly expensive
    _win32_logicaldisk = {}

    def _get_win32_logicaldisk(self, drive):
        """ Return the Win32_LogicalDisk object for the given drive """
        import win32com.client
        cache = self._win32_logicaldisk.get('drive')
        if cache:
            return cache
        obj = None
        try:
            obj = win32com.client.Dispatch("WbemScripting.SWbemLocator") \
                         .ConnectServer(".", "root\cimv2") \
                         .ExecQuery("Select * from "
                                    "Win32_LogicalDisk where Name = '%s'" %
                                    drive)
            if not obj:
                self.log.error(_("Unable to get Win32_LogicalDisk; win32com "
                                 "query did not return any results"))
            else:
                obj = obj[0]
                self._win32_logicaldisk[drive] = obj
        except Exception, e:
            self.log.exception(e)
            self.log.error("Unable to get Win32_LogicalDisk")
        return obj

    def _get_device_uuid(self, drive):
        """ Return the UUID of our selected drive """
        if self.uuid:
            return self.uuid
        uuid = None
        try:
            uuid = self._get_win32_logicaldisk(drive).VolumeSerialNumber
            if uuid in (None, 'None', ''):
                uuid = None
            else:
                uuid = uuid[:4] + '-' + uuid[4:]
            self.log.debug(_("Found UUID %s for %s") % (uuid, drive))
        except Exception, e:
            self.log.exception(e)
            self.log.warning(_("Exception while fetching UUID: %r") % e)
        return uuid

    def _get_device_size(self, drive):
        """ Return the size of the given drive """
        size = None
        try:
            size = int(self._get_win32_logicaldisk(drive).Size)
            self.log.debug(_("Max size of %s: %d") % (drive, size))
        except Exception, e:
            self.log.exception(e)
            self.log.warning(_("Error getting drive size: %r") % e)
        return size

    def popen(self, cmd, **kwargs):
        import win32process
        if isinstance(cmd, basestring):
            cmd = cmd.split()
        prgmfiles = os.getenv('PROGRAMFILES')
        folder = 'LiveUSB Creator'
        paths = [os.path.join(x, folder) for x in (prgmfiles, prgmfiles + ' (x86)')]
        paths += [os.path.join(os.path.dirname(__file__), '..', '..'), '.']
        tool = None
        for path in paths:
            exe = os.path.join(path, 'tools', '%s.exe' % cmd[0])
            if os.path.exists(exe):
                tool = '"%s"' % exe
                break
        else:
            raise LiveUSBError(_("Cannot find") + ' %s.  ' % (cmd[0]) +
                               _("Make sure to extract the entire "
                                 "liveusb-creator zip file before "
                                 "running this program."))
        return LiveUSBCreator.popen(self, ' '.join([tool] + cmd[1:]),
                                    creationflags=win32process.CREATE_NO_WINDOW,
                                    **kwargs)

    def terminate(self):
        """ Terminate any subprocesses that we have spawned """
        import win32api, win32con, pywintypes
        for pid in self.pids:
            try:
                handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE,
                                              False, pid)
                self.log.debug("Terminating process %s" % pid)
                win32api.TerminateProcess(handle, -2)
                win32api.CloseHandle(handle)
            except pywintypes.error:
                pass

    def mount_device(self):
        self.dest = self.drive['mount']

    def unmount_device(self):
        pass

    def get_proxies(self):
        proxies = {}
        try:
            import _winreg as winreg
            settings = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                      'Software\\Microsoft\\Windows'
                                      '\\CurrentVersion\\Internet Settings')
            proxy = winreg.QueryValueEx(settings, "ProxyEnable")[0]
            if proxy:
                server = str(winreg.QueryValueEx(settings, 'ProxyServer')[0])
                if ';' in server:
                    for p in server.split(';'):
                        protocol, address = p.split('=')
                        proxies[protocol] = '%s://%s' % (protocol, address)
                else:
                    proxies['http'] = 'http://%s' % server
                    proxies['ftp'] = 'ftp://%s' % server
            settings.Close()
        except Exception, e:
            self.log.warning(_('Unable to detect proxy settings: %r') % e)
        self.log.debug(_('Using proxies: %r') % proxies)
        return proxies

    def verify_iso_md5(self):
        """ Verify the ISO md5sum.

        At the moment this is Linux-only, until we port checkisomd5 to Windows.
        """
        return True

    def calculate_device_checksum(self, progress=None):
        """ Calculate the SHA1 checksum of the device """
        self.log.info(_("Calculating the SHA1 of %s" % self._drive))
        time.sleep(3)
        if not progress:
            class DummyProgress:
                def set_max_progress(self, value): pass
                def update_progress(self, value): pass
            progress = DummyProgress()
        progress.set_max_progress(self.drive['size'])
        checksum = hashlib.sha1()
        device_name = r'\\.\%s' % self.drive['device']
        device = file(device_name, 'rb')
        bytes = 1
        total = 0
        while bytes:
            data = device.read(1024**2)
            checksum.update(data)
            bytes = len(data)
            total += bytes
            progress.update_progress(total)
        hexdigest = checksum.hexdigest()
        self.log.info("sha1(%s) = %s" % (self.drive['device'], hexdigest))
        return hexdigest

    def format_device(self):
        """ Format the selected partition as FAT32 """
        self.log.info('Formatting %s as FAT32' % self.drive['device'])
        self.popen('format /Q /X /y /V:Fedora /FS:FAT32 %s' % self.drive['device'])

    def is_admin(self):
        import pywintypes
        try:
            from win32com.shell import shell
            return shell.IsUserAnAdmin()
        except pywintypes.com_error:
            # Thrown on certain XP installs
            return True
