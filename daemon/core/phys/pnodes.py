"""
PhysicalNode class for including real systems in the emulated network.
"""

import os
import subprocess
import threading

from core import constants
from core import logger
from core.coreobj import PyCoreNode
from core.misc import utils
from core.netns.vnet import GreTap
from core.netns.vnet import LxBrNet


class PhysicalNode(PyCoreNode):
    def __init__(self, session, objid=None, name=None, nodedir=None, start=True):
        PyCoreNode.__init__(self, session, objid, name, start=start)
        self.nodedir = nodedir
        self.up = start
        self.lock = threading.RLock()
        self._mounts = []
        if start:
            self.startup()

    def boot(self):
        self.session.services.bootnodeservices(self)

    def validate(self):
        self.session.services.validatenodeservices(self)

    def startup(self):
        self.lock.acquire()
        try:
            self.makenodedir()
            # self.privatedir("/var/run")
            # self.privatedir("/var/log")
        except OSError:
            logger.exception("startup error")
        finally:
            self.lock.release()

    def shutdown(self):
        if not self.up:
            return
        self.lock.acquire()
        while self._mounts:
            source, target = self._mounts.pop(-1)
            self.umount(target)
        for netif in self.netifs():
            netif.shutdown()
        self.rmnodedir()
        self.lock.release()

    def termcmdstring(self, sh="/bin/sh"):
        """
        The broker will add the appropriate SSH command to open a terminal
        on this physical node.
        """
        return sh

    def cmd(self, args, wait=True):
        """
        run a command on the physical node
        """
        os.chdir(self.nodedir)
        try:
            if wait:
                # os.spawnlp(os.P_WAIT, args)
                subprocess.call(args)
            else:
                # os.spawnlp(os.P_NOWAIT, args)
                subprocess.Popen(args)
        except subprocess.CalledProcessError:
            logger.exception("cmd exited with status: %s", str(args))

    def cmdresult(self, args):
        """
        run a command on the physical node and get the result
        """
        os.chdir(self.nodedir)
        # in Python 2.7 we can use subprocess.check_output() here
        tmp = subprocess.Popen(args, stdin=open(os.devnull, 'r'),
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
        # err will always be None
        result, err = tmp.communicate()
        status = tmp.wait()
        return status, result

    def shcmd(self, cmdstr, sh="/bin/sh"):
        return self.cmd([sh, "-c", cmdstr])

    def sethwaddr(self, ifindex, addr):
        """
        same as SimpleLxcNode.sethwaddr()
        """
        self._netif[ifindex].sethwaddr(addr)
        ifname = self.ifname(ifindex)
        if self.up:
            (status, result) = self.cmdresult(
                [constants.IP_BIN, "link", "set", "dev", ifname, "address", str(addr)])
            if status:
                logger.error("error setting MAC address %s", str(addr))

    def addaddr(self, ifindex, addr):
        """
        same as SimpleLxcNode.addaddr()
        """
        if self.up:
            self.cmd([constants.IP_BIN, "addr", "add", str(addr), "dev", self.ifname(ifindex)])

        self._netif[ifindex].addaddr(addr)

    def deladdr(self, ifindex, addr):
        """
        same as SimpleLxcNode.deladdr()
        """
        try:
            self._netif[ifindex].deladdr(addr)
        except ValueError:
            logger.exception("trying to delete unknown address: %s", addr)

        if self.up:
            self.cmd([constants.IP_BIN, "addr", "del", str(addr), "dev", self.ifname(ifindex)])

    def adoptnetif(self, netif, ifindex, hwaddr, addrlist):
        """
        The broker builds a GreTap tunnel device to this physical node.
        When a link message is received linking this node to another part of
        the emulation, no new interface is created; instead, adopt the
        GreTap netif as the node interface.
        """
        netif.name = "gt%d" % ifindex
        netif.node = self
        self.addnetif(netif, ifindex)
        # use a more reasonable name, e.g. "gt0" instead of "gt.56286.150"
        if self.up:
            self.cmd([constants.IP_BIN, "link", "set", "dev", netif.localname, "down"])
            self.cmd([constants.IP_BIN, "link", "set", netif.localname, "name", netif.name])
        netif.localname = netif.name
        if hwaddr:
            self.sethwaddr(ifindex, hwaddr)
        for addr in utils.maketuple(addrlist):
            self.addaddr(ifindex, addr)
        if self.up:
            self.cmd([constants.IP_BIN, "link", "set", "dev", netif.localname, "up"])

    def linkconfig(self, netif, bw=None, delay=None,
                   loss=None, duplicate=None, jitter=None, netif2=None):
        """
        Apply tc queing disciplines using LxBrNet.linkconfig()
        """
        # borrow the tc qdisc commands from LxBrNet.linkconfig()
        linux_bridge = LxBrNet(session=self.session, start=False)
        linux_bridge.up = True
        linux_bridge.linkconfig(netif, bw=bw, delay=delay, loss=loss, duplicate=duplicate,
                                jitter=jitter, netif2=netif2)
        del linux_bridge

    def newifindex(self):
        with self.lock:
            while self.ifindex in self._netif:
                self.ifindex += 1
            ifindex = self.ifindex
            self.ifindex += 1
            return ifindex

    def newnetif(self, net=None, addrlist=None, hwaddr=None, ifindex=None, ifname=None):
        logger.info("creating interface")
        if not addrlist:
            addrlist = []

        if self.up and net is None:
            raise NotImplementedError

        if ifindex is None:
            ifindex = self.newifindex()

        if self.up:
            # this is reached when this node is linked to a network node
            # tunnel to net not built yet, so build it now and adopt it
            gt = self.session.broker.addnettunnel(net.objid)
            if gt is None or len(gt) != 1:
                raise ValueError("error building tunnel from adding a new network interface: %s" % gt)
            gt = gt[0]
            net.detach(gt)
            self.adoptnetif(gt, ifindex, hwaddr, addrlist)
            return ifindex

        # this is reached when configuring services (self.up=False)
        if ifname is None:
            ifname = "gt%d" % ifindex

        netif = GreTap(node=self, name=ifname, session=self.session, start=False)
        self.adoptnetif(netif, ifindex, hwaddr, addrlist)
        return ifindex

    def privatedir(self, path):
        if path[0] != "/":
            raise ValueError, "path not fully qualified: " + path
        hostpath = os.path.join(self.nodedir, os.path.normpath(path).strip('/').replace('/', '.'))
        try:
            os.mkdir(hostpath)
        except OSError:
            logger.exception("error creating directory: %s", hostpath)

        self.mount(hostpath, path)

    def mount(self, source, target):
        source = os.path.abspath(source)
        logger.info("mounting %s at %s" % (source, target))

        try:
            os.makedirs(target)
            self.cmd([constants.MOUNT_BIN, "--bind", source, target])
            self._mounts.append((source, target))
        except OSError:
            logger.exception("error making directories")
        except:
            logger.exception("mounting failed for %s at %s", source, target)

    def umount(self, target):
        logger.info("unmounting '%s'" % target)
        try:
            self.cmd([constants.UMOUNT_BIN, "-l", target])
        except:
            logger.exception("unmounting failed for %s", target)

    def opennodefile(self, filename, mode="w"):
        dirname, basename = os.path.split(filename)
        if not basename:
            raise ValueError("no basename for filename: " + filename)
        if dirname and dirname[0] == "/":
            dirname = dirname[1:]
        dirname = dirname.replace("/", ".")
        dirname = os.path.join(self.nodedir, dirname)
        if not os.path.isdir(dirname):
            os.makedirs(dirname, mode=0755)
        hostfilename = os.path.join(dirname, basename)
        return open(hostfilename, mode)

    def nodefile(self, filename, contents, mode=0644):
        f = self.opennodefile(filename, "w")
        f.write(contents)
        os.chmod(f.name, mode)
        f.close()
        logger.info("created nodefile: '%s'; mode: 0%o" % (f.name, mode))
