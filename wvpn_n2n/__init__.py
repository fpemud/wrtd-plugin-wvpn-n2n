#!/usr/bin/python3
# -*- coding: utf-8; tab-width: 4; indent-tabs-mode: t -*-

import os
import pwd
import grp
import time
import socket
import logging
import netifaces
import ipaddress
import threading
import subprocess
from gi.repository import GLib
from gi.repository import GObject


def get_plugin_list():
    return ["n2n"]


def get_plugin(name):
    if name == "n2n":
        return _PluginObject()
    else:
        assert False


class _PluginObject:

    def init2(self, cfg, tmpDir, upCallback, downCallback):
        self.cfg = cfg
        self.vpnIntfName = "vpnc"
        self.tmpDir = tmpDir
        self.upCallback = upCallback
        self.downCallback = downCallback
        self.logger = logging.getLogger(self.__module__ + "." + self.__class__.__name__)

        self.vpnRestartInterval = 60        # in seconds
        self.vpnRestartTimer = None

        self.vpnCheckInterval = 10          # in seconds
        self.vpnCheckTimer = None

        self.dhcpClientProc = None
        self.dhcpClientProcWatch = None

        self.localIp = None
        self.remoteIp = None
        self.netmask = None
        self.waitIpThread = None

    def start(self):
        self.vpnRestartTimer = GObject.timeout_add_seconds(0, self._vpnRestartTimerCallback)

    def stop(self):
        if self.vpnIntfName in netifaces.interfaces():
            if self.localIp is not None:
                self.downCallback()
            self._vpnStop()
            self.logger.info("CASCADE-VPN disconnected.")
        else:
            if self.vpnRestartTimer is not None:
                GLib.source_remove(self.vpnRestartTimer)
                self.vpnRestartTimer = None

    def disconnect(self):
        # disconnect by killing dhclient process, it is faster than killing n2n-edge process
        if self.dhcpClientProc is not None:
            if self.dhcpClientProc.poll() is None:
                self._stopDhcpClient()

    def is_connected(self):
        return self.vpnIntfName in netifaces.interfaces()

    def get_local_ip(self):
        return self.localIp

    def get_remote_ip(self):
        return self.remoteIp

    def get_netmask(self):
        return self.netmask

    def get_interface(self):
        return self.vpnIntfName

    def _vpnRestartTimerCallback(self):
        self.logger.info("Establishing CASCADE-VPN connection.")
        try:
            self._vpnStart()
            self.vpnRestartTimer = None
        except Exception as e:
            self._vpnStop()
            self.logger.error("Failed to establish CASCADE-VPN connection, %s", e)
            self.vpnRestartTimer = GObject.timeout_add_seconds(self.vpnRestartInterval, self._vpnRestartTimerCallback)
        finally:
            return False

    def _vpnCheckTimerCallback(self):
        # it is a bad function
        # we should add child-watch to /usr/sbin/edge but /usr/sbin/edge forks itself to an unknown pid

        if self.vpnIntfName in netifaces.interfaces():
            return True

        bFlag = False
        if self.localIp is not None:
            self.downCallback()
            bFlag = True
        self._vpnStop()
        if bFlag:
            self.logger.info("CASCADE-VPN disconnected.")
        else:
            self.logger.error("Failed to establish CASCADE-VPN connection")
        self.vpnRestartTimer = GObject.timeout_add_seconds(self.vpnRestartInterval, self._vpnRestartTimerCallback)
        return False

    def _vpnChildWatchCallback(self, pid, condition):
        assert pid == self.dhcpClientProc.pid

        bFlag = False
        if self.localIp is not None:
            self.downCallback()
            bFlag = True
        self._vpnStop()
        if bFlag:
            self.logger.info("CASCADE-VPN disconnected.")
        else:
            self.logger.error("Failed to establish CASCADE-VPN connection")
        self.vpnRestartTimer = GObject.timeout_add_seconds(self.vpnRestartInterval, self._vpnRestartTimerCallback)

    def _vpnStart(self):
        # run n2n edge process
        # it sucks that /usr/sbin/edge forks itself to a new process can there's no way for us to know the PID of the new process.
        # so we use its management interface (UDP 5644) to control /usr/sbin/edge.
        # it also sucks that we can run only one edge process in a machine, so wrtd-plugin-wvpn-n2n and wrtd-plugin-vpns-n2n is mutal-exclusive.
        cmd = "/usr/sbin/edge -f "
        cmd += "-l %s " % (self.cfg["supernode"])
        cmd += "-r -a dhcp:0.0.0.0 "
        cmd += "-d %s " % (self.vpnIntfName)
        cmd += "-c %s " % (self.cfg["community"])
        cmd += "-k %s " % (self.cfg["key"])
        cmd += "-u %d -g %d " % (pwd.getpwnam("nobody").pw_uid, grp.getgrnam("nobody").gr_gid)
        cmd += ">%s 2>&1" % (os.path.join(self.tmpDir, "edge.out"))
        subprocess.Popen(cmd, shell=True, universal_newlines=True)

        # wait for interface
        i = 0
        while True:
            if self.vpnIntfName not in netifaces.interfaces():
                if i >= 10:
                    raise Exception("Interface allocation time out.")
                time.sleep(1.0)
                i += 1
                continue
            break

        # create dhclient.conf, copied from nm-dhcp-dhclient-utils.c in networkmanager-1.4.4
        cfgf = os.path.join(self.tmpDir, "dhclient.conf")
        with open(cfgf, "w") as f:
            buf = ""
            buf += "send host-name \"%s\";\n" % (socket.gethostname())
            buf += "\n"
            buf += "option rfc3442-classless-static-routes code 121 = array of unsigned integer 8;\n"
            buf += "option wpad code 252 = string;\n"
            buf += "\n"
            buf += "also request rfc3442-classless-static-routes;\n"
            buf += "also request static-routes;\n"
            buf += "also request wpad;\n"
            buf += "also request ntp-servers;\n"
            buf += "\n"
            buf += "supersede routers 0.0.0.0;\n"               # reject, no way to remove an option, it is just a workaround, dhclient sucks
            f.write(buf)

        self.dhcpClientProc = subprocess.Popen([
            "/usr/bin/python3",
            os.path.join(os.path.dirname(os.path.realpath(__file__)), "subproc_dhclient.py"),
            self.tmpDir,
            cfgf,
            self.vpnIntfName,
        ])

        # start wait ip thread
        self.waitIpThread = _WaitIpThread(self)
        self.waitIpThread.start()

        # start child watch
        self.vpnCheckTimer = GObject.timeout_add_seconds(self.vpnCheckInterval, self._vpnCheckTimerCallback)
        self.dhcpClientProcWatch = GLib.child_watch_add(self.dhcpClientProc.pid, self._vpnChildWatchCallback)

    def _vpnStop(self):
        self.netmask = None
        self.remoteIp = None
        self.localIp = None

        if self.dhcpClientProcWatch is not None:
            GLib.source_remove(self.dhcpClientProcWatch)
            self.dhcpClientProcWatch = None
        if self.vpnCheckTimer is not None:
            GLib.source_remove(self.vpnCheckTimer)
            self.vpnCheckTimer = None

        if self.waitIpThread is not None:
            self.waitIpThread.stop()
            self.waitIpThread.join()
            self.waitIpThread = None

        if self.dhcpClientProc is not None:
            if self.dhcpClientProc.poll() is None:
                self._stopDhcpClient()
                self.dhcpClientProc.wait()
            self.dhcpClientProc = None

        if self.vpnIntfName in netifaces.interfaces():
            self._stopEdge()
            while self.vpnIntfName in netifaces.interfaces():
                time.sleep(1.0)

    def _vpnUpCallback(self):
        try:
            t = netifaces.ifaddresses(self.vpnIntfName)
            self.localIp = t[netifaces.AF_INET][0]["addr"]
            self.remoteIp = ".".join(self.localIp.split(".")[:3] + ["1"])         # trick
            self.netmask = t[netifaces.AF_INET][0]["netmask"]
            self.logger.info("CASCADE-VPN connected.")
        except Exception as e:
            self._vpnStop()
            self.logger.error("Failed to establish CASCADE-VPN connection, %s", e)
            self.vpnRestartTimer = GObject.timeout_add_seconds(self.vpnRestartInterval, self._vpnRestartTimerCallback)

        try:
            self.upCallback()
        except Exception as e:
            self._vpnStop()
            self.logger.error("CASCADE-VPN disconnected because internal error occured, %s", e)
            self.vpnRestartTimer = GObject.timeout_add_seconds(self.vpnRestartInterval, self._vpnRestartTimerCallback)

    def _stopEdge(self):
        # kill n2n-edge process using control channel
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto("stop".encode("utf-8"), ('127.0.0.1', 5644))
        s.close()

    def _stopDhcpClient(self):
        # use "dhclient -r" to kill dhclient process and release ip address
        subprocess.Popen([
            "/usr/bin/python3",
            os.path.join(os.path.dirname(os.path.realpath(__file__)), "subproc_dhclient_release.py"),
            self.tmpDir,
            os.path.join(self.tmpDir, "dhclient.conf"),
            self.vpnIntfName,
        ]).wait()

        # wait for the release packet to be sent out
        time.sleep(1.0)


class _WaitIpThread(threading.Thread):

    def __init__(self, pObj):
        super().__init__()
        self.pObj = pObj
        self.bStop = False

    def run(self):
        count = 0
        while not self.bStop:
            if netifaces.AF_INET in netifaces.ifaddresses(self.pObj.vpnIntfName):
                count += 1
            else:
                count = 0
            if count >= 3:
                _Util.idleInvoke(self.pObj._vpnUpCallback)      # ip address must be stablized for 3 seconds
                break
            time.sleep(1.0)
        self.pObj.waitIpThread = None

    def stop(self):
        self.bStop = True


class _Util:

    @staticmethod
    def idleInvoke(func, *args):
        def _idleCallback(func, *args):
            func(*args)
            return False
        GLib.idle_add(_idleCallback, func, *args)
