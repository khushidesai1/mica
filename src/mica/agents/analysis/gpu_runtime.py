"""Trusted setup on a dedicated GPU VM, before the unprivileged worker starts."""

# NVIDIA's legacy hook overmounts /proc/driver/nvidia, preventing a nested user/PID
# namespace from mounting private procfs. Configure both JIT-CDI and Docker's
# native CDI path to omit only that metadata hook. Device access remains restricted by Docker's
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
refresh = Path('/etc/nvidia-container-toolkit/nvidia-cdi-refresh.env')
refresh.parent.mkdir(parents=True, exist_ok=True)
settings = {
    'NVIDIA_CTK_CDI_GENERATE_DISABLED_HOOKS': 'disable-device-node-modification',
    'NVIDIA_CTK_CDI_OUTPUT_FILE_PATH': '/var/run/cdi/nvidia.yaml',
}
lines = refresh.read_text().splitlines() if refresh.exists() else []
lines = [line for line in lines if line.split('=', 1)[0] not in settings]
refresh.write_text('\\n'.join(lines + [f'{k}={v}' for k, v in settings.items()]) + '\\n')
PY
systemctl daemon-reload
systemctl restart nvidia-cdi-refresh.service
# Generate and validate explicitly. The CDI writer chooses serialization from the
# filename extension even when --format is set, so use .json outside CDI's search
# directories and atomically replace the standard file after validation.
mkdir -p /run/mica-gpu-cdi
nvidia-ctk cdi generate --disable-hook disable-device-node-modification \\
  --format=json --output=/run/mica-gpu-cdi/nvidia.json
python3 - <<'PY'
import json
from pathlib import Path
# JSON is valid YAML; retaining the service's standard filename replaces the
# existing spec rather than leaving conflicting duplicate device definitions.
candidate = Path('/run/mica-gpu-cdi/nvidia.json')
spec = json.loads(candidate.read_text())
assert spec['kind'] == 'nvidia.com/gpu'
assert any(device['name'] == '0' for device in spec['devices'])
edits = [spec.get('containerEdits', {})] + [d.get('containerEdits', {}) for d in spec['devices']]
hooks = [hook for edit in edits for hook in edit.get('hooks', [])]
assert not any('disable-device-node-modification' in h.get('args', []) for h in hooks)
assert any('update-ldcache' in h.get('args', []) for h in hooks)
assert not any(m['containerPath'].startswith('/proc/') for e in edits for m in e.get('mounts', []))
candidate.replace('/var/run/cdi/nvidia.yaml')
print('MICA_GPU_RUNTIME_CONFIGURED', flush=True)
PY
systemctl restart docker
docker version --format '{{.Server.Version}}'
"""

OPTIONS = " --runtime=nvidia --user=65532:65532 --cap-drop=ALL --security-opt no-new-privileges"
