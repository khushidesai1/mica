"""Trusted setup on a dedicated GPU VM, before the unprivileged worker starts."""

# NVIDIA's legacy hook overmounts /proc/driver/nvidia, preventing a nested user/PID
# namespace from mounting private procfs. JIT-CDI avoids the legacy mounts; omit
# only its equivalent metadata hook. Device access remains restricted by Docker's
# device cgroup and the application's fixed device allowlist. The worker has no
# capabilities (including MKNOD), cannot gain privileges, and never runs as root.
SETUP = """set -eu
nvidia-ctk config --in-place \\
  --set nvidia-container-runtime.mode=jit-cdi \\
  --set nvidia-container-runtime.modes.jit-cdi.nvcdi-disable-hooks=disable-device-node-modification
nvidia-ctk runtime configure --runtime=docker
python3 - <<'PY'
import json
import re
import subprocess
import tomllib
from pathlib import Path
version = subprocess.check_output(['nvidia-ctk', '--version'], text=True)
match = re.search(r'(\\d+)\\.(\\d+)\\.(\\d+)', version)
assert match and tuple(map(int, match.groups())) >= (1, 20, 0), version
config = tomllib.loads(Path('/etc/nvidia-container-runtime/config.toml').read_text())
runtime = config['nvidia-container-runtime']
assert runtime['mode'] == 'jit-cdi'
assert runtime['modes']['jit-cdi']['nvcdi-disable-hooks'] == ['disable-device-node-modification']
docker = json.loads(Path('/etc/docker/daemon.json').read_text())
assert 'nvidia' in docker['runtimes']
print('MICA_GPU_RUNTIME_CONFIGURED', flush=True)
PY
systemctl restart docker
"""

OPTIONS = " --runtime=nvidia --user=65532:65532 --cap-drop=ALL --security-opt no-new-privileges"
