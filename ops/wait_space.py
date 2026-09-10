import sys, time
sys.path.insert(0, "/mnt/users/s8sharif/mast")
from ops.deploy import load_env
from huggingface_hub import HfApi
api = HfApi(token=load_env()["HF_TOKEN"]); t0 = time.time(); time.sleep(6); stage = None
while time.time() - t0 < 420:
    stage = api.get_space_runtime("sahel-sh/submit").stage
    if stage in ("RUNNING", "RUNTIME_ERROR", "BUILD_ERROR", "CONFIG_ERROR"): break
    time.sleep(8)
print(f"space stage after {time.time()-t0:.0f}s: {stage}")
if stage != "RUNNING": sys.exit(1)
