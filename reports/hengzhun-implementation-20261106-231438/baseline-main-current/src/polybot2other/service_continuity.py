"""Process-scoped idle-sleep protection and interruption recovery, without system setting changes."""
import os
import subprocess
import sys
import threading
import time


class ServiceContinuity:
    def __init__(self,enabled=False,on_resume=None,platform_name=None):
        self.enabled=enabled;self.supported=(platform_name or sys.platform)=="darwin";self.on_resume=on_resume
        self.lock=threading.RLock();self.stop_event=threading.Event();self.thread=None;self.guard=None
        self.last_wall=None;self.last_monotonic=None;self.next_guard_retry=0.
        self.error=None;self.last_gap=None;self.recovery_error=None

    def _ensure_guard(self):
        if not self.enabled or not self.supported:return
        with self.lock:
            if self.guard and self.guard.poll() is None:return
            if self.guard:self.error="防自动休眠进程已退出，正在重新启动"
            if time.time()<self.next_guard_retry:return
            self.next_guard_retry=time.time()+30
            try:
                self.guard=subprocess.Popen(["/usr/bin/caffeinate","-i","-s","-w",str(os.getpid())],
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
                self.error=None
            except Exception as exc:self.error=f"防自动休眠未启用（{type(exc).__name__}）"

    def observe(self,wall=None,monotonic=None):
        wall=time.time() if wall is None else wall;monotonic=time.monotonic() if monotonic is None else monotonic
        gap=None
        with self.lock:
            if self.last_wall is not None:
                elapsed=wall-self.last_wall;steady=monotonic-self.last_monotonic
                if elapsed>30 or abs(elapsed-steady)>15:
                    gap=max(abs(elapsed),abs(elapsed-steady));self.last_gap={"from":self.last_wall,"to":wall,"seconds":round(gap,1)}
                    self.next_guard_retry=0
            self.last_wall=wall;self.last_monotonic=monotonic
        if gap is not None and self.on_resume:
            try:self.on_resume(gap);self.recovery_error=None
            except Exception as exc:self.recovery_error=type(exc).__name__

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear();self._ensure_guard();self.observe()
        def monitor():
            while not self.stop_event.wait(2):
                self.observe();self._ensure_guard()
        self.thread=threading.Thread(target=monitor,name="service-continuity",daemon=True);self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():self.thread.join(timeout=3)
        with self.lock:
            if self.guard and self.guard.poll() is None:
                self.guard.terminate()
                try:self.guard.wait(timeout=2)
                except subprocess.TimeoutExpired:self.guard.kill();self.guard.wait(timeout=2)
            self.guard=None

    def snapshot(self):
        with self.lock:
            active=bool(self.guard and self.guard.poll() is None)
            return {"enabled":self.enabled,"supported":self.supported,"keep_awake_active":active,
                    "guard_pid":self.guard.pid if active else None,"error":self.error,
                    "last_heartbeat_at":self.last_wall,"last_interruption":self.last_gap,"recovery_error":self.recovery_error,
                    "note":"防自动休眠随服务运行；屏幕可熄灭，请保持开盖。合盖或手动睡眠仍可能中断服务。"}
