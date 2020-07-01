#!/usr/bin/env python3.7
import time
import subprocess
import cereal
import cereal.messaging as messaging
ThermalStatus = cereal.log.ThermalData.ThermalStatus
from selfdrive.swaglog import cloudlog
from common.realtime import sec_since_boot
from common.params import Params, put_nonblocking
params = Params()
from math import floor
import re
import os

class App():

  # app type
  TYPE_GPS = 0
  TYPE_SERVICE = 1
  TYPE_GPS_SERVICE = 2
  TYPE_FULLSCREEN = 3
  TYPE_UTIL = 4

  # manual switch stats
  MANUAL_OFF = "-1"
  MANUAL_IDLE = "0"
  MANUAL_ON = "1"

  def appops_set(self, package, op, mode):
    self.system(f"LD_LIBRARY_PATH= appops set {package} {op} {mode}")

  def pm_grant(self, package, permission):
    self.system(f"pm grant {package} {permission}")

  def set_package_permissions(self):
    if self.permissions is not None:
      for permission in self.permissions:
        self.pm_grant(self.app, permission)
    if self.opts is not None:
      for opt in self.opts:
        self.appops_set(self.app, opt, "allow")

  def __init__(self, app, activity, enable_param, auto_run_param, manual_ctrl_param, app_type, permissions, opts):
    self.app = app
    # main activity
    self.activity = activity
    # read enable param
    self.enable_param = enable_param
    # read auto run param
    self.auto_run_param = auto_run_param
    # read manual run param
    self.manual_ctrl_param = manual_ctrl_param
    # if it's a service app, we do not kill if device is too hot
    self.app_type = app_type
    # app permissions
    self.permissions = permissions
    # app options
    self.opts = opts

    self.is_installed = False
    self.is_enabled = True if params.get(self.enable_param, encoding='utf8') == "1" else False
    self.last_is_enabled = False
    self.is_auto_runnable = False
    self.is_running = False
    self.manual_ctrl_status = self.MANUAL_IDLE
    self.manually_ctrled = False

    if self.is_enabled:
      local_version = self.get_local_version()
      if local_version is not None:
        self.is_installed = True

      if is_online:
        remote_version = local_version
        if local_version is not None and auto_update:
          remote_version = self.get_remote_version()
        if local_version is None or (remote_version is not None and local_version != remote_version):
          self.update_app()
      if self.is_installed:
        self.set_package_permissions()
    else:
      self.uninstall_app()

    if self.manual_ctrl_param is not None:
      put_nonblocking(self.manual_ctrl_param, '0')
    self.last_ts = sec_since_boot()

  def run(self, force = False):
    if self.is_installed and (force or self.is_enabled):
      # app is manually ctrl, we record that
      if self.manual_ctrl_param is not None and self.manual_ctrl_status == self.MANUAL_ON:
        put_nonblocking(self.manual_ctrl_param, '0')
        self.manually_ctrled = True
        self.is_running = False

      # only run app if it's not running
      if force or not self.is_running:
        self.system("pm enable %s" % self.app)

        if self.app_type == self.TYPE_GPS_SERVICE:
          self.appops_set(self.app, "android:mock_location", "allow")

        if self.app_type in [self.TYPE_SERVICE, self.TYPE_GPS_SERVICE]:
          self.system("am startservice %s/%s" % (self.app, self.activity))
        else:
          self.system("am start -n %s/%s" % (self.app, self.activity))
    self.is_running = True

  def kill(self, force = False):
    if self.is_installed and (force or self.is_enabled):
      # app is manually ctrl, we record that
      if self.manual_ctrl_param is not None and self.manual_ctrl_status == self.MANUAL_OFF:
        put_nonblocking(self.manual_ctrl_param, '0')
        self.manually_ctrled = True
        self.is_running = True

      # only kill app if it's running
      if force or self.is_running:
        if self.app_type == self.TYPE_GPS_SERVICE:
          self.appops_set(self.app, "android:mock_location", "deny")

        self.system("pkill %s" % self.app)
        self.is_running = False

  def system(self, cmd):
    try:
      subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=True)
    except subprocess.CalledProcessError as e:
      cloudlog.event("running failed",
                     cmd=e.cmd,
                     output=e.output[-1024:],
                     returncode=e.returncode)

def init_apps(apps):
  apps.append(App(
    "com.mixplorer",
    "com.mixplorer.activities.BrowseActivity",
    [],
    None,
    "OpkrRunMixplorer",
    App.TYPE_UTIL,
    [
      "android.permission.READ_EXTERNAL_STORAGE",
      "android.permission.WRITE_EXTERNAL_STORAGE",
    ],
    [],
  ))
  apps.append(App(
    "com.quickedit",
    "com.quickedit.activities.BrowseActivity",
    [],
    None,
    "OpkrRunQuickedit",
    App.TYPE_UTIL,
    [
      "android.permission.READ_EXTERNAL_STORAGE",
      "android.permission.WRITE_EXTERNAL_STORAGE",
    ],
    [],
  ))

def main():
  apps = []

  last_started = False
  thermal_sock = messaging.sub_sock('thermal')

  frame = 0
  start_delay = None
  stop_delay = None
  allow_auto_run = True
  last_thermal_status = None
  thermal_status = None
  start_ts = sec_since_boot()
  init_done = False
  last_modified = None

  while 1: #has_enabled_apps:
    if not init_done:
      if sec_since_boot() - start_ts >= 10:
        init_apps(apps)
        init_done = True
    else:
      enabled_apps = []
      has_fullscreen_apps = False
      modified = get_last_modified()
      for app in apps:
        # read params loop
        if last_modified != modified:
          app.read_params()
        if app.last_is_enabled and not app.is_enabled and app.is_running:
          app.kill(True)

        if app.is_enabled:
          if not has_fullscreen_apps and app.app_type == App.TYPE_FULLSCREEN:
            has_fullscreen_apps = True

          # process manual ctrl apps
          if app.manual_ctrl_status != App.MANUAL_IDLE:
            app.run(True) if app.manual_ctrl_status == App.MANUAL_ON else app.kill(True)

          enabled_apps.append(app)
      last_modified = modified
      msg = messaging.recv_sock(thermal_sock, wait=True)
      started = msg.thermal.started
      # when car is running
      if started:
        stop_delay = None
        # apps start 5 secs later
        if start_delay is None:
          start_delay = frame + 5

        thermal_status = msg.thermal.thermalStatus
        if thermal_status <= ThermalStatus.yellow:
          allow_auto_run = True
          # when temp reduce from red to yellow, we add start up delay as well
          # so apps will not start up immediately
          if last_thermal_status == ThermalStatus.red:
            start_delay = frame + 60
        elif thermal_status >= ThermalStatus.red:
          allow_auto_run = False

        last_thermal_status = thermal_status

        # we run service apps and kill all util apps
        # only run once
        if last_started != started:
          for app in enabled_apps:
            if app.app_type in [App.TYPE_SERVICE, App.TYPE_GPS_SERVICE]:
              app.run()
            elif app.app_type == App.TYPE_UTIL:
              app.kill()

        # only run apps that's not manually ctrled
        for app in enabled_apps:
          if not app.manually_ctrled:
            if has_fullscreen_apps:
              if app.app_type == App.TYPE_FULLSCREEN:
                app.run()
              elif app.app_type in [App.TYPE_GPS, App.TYPE_UTIL]:
                app.kill()
            else:
              if not allow_auto_run:
                app.kill()
              else:
                if frame >= start_delay and app.is_auto_runnable and app.app_type == App.TYPE_GPS:
                  app.run()
      # when car is stopped
      else:
        start_delay = None
        # set delay to 30 seconds
        if stop_delay is None:
          stop_delay = frame + 30

        for app in enabled_apps:
          if app.is_running and not app.manually_ctrled:
            if has_fullscreen_apps or frame >= stop_delay:
              app.kill()

      if last_started != started:
        for app in enabled_apps:
          app.manually_ctrled = False

      last_started = started
      frame += 3
    time.sleep(3)

if __name__ == "__main__":
  main()
