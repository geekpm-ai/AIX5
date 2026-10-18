from __future__ import annotations
import time

try:
    import psutil
except:
    psutil = None


class Metrics:
    def snapshot(self):
        d = {
            "timestamp": time.time(),
            "cpu_percent": psutil.cpu_percent(interval=None) if psutil else None,
            "memory_percent": psutil.virtual_memory().percent if psutil else None,
            "disk_percent": psutil.disk_usage("/").percent if psutil else None,
        }
        for p in [
            "/sys/devices/system/bpu/bpu0/ratio",
            "/sys/class/misc/bpu_core0/ratio",
        ]:
            try:
                with open(p) as f:
                    d["bpu_ratio"] = f.read().strip()
                break
            except:
                pass
        return d


metrics = Metrics()
