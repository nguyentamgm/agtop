#!/bin/sh
# Create a private venv next to agtop.py and install a launcher at ~/.local/bin/agtop.
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
if command -v uv >/dev/null 2>&1; then
  uv venv "$DIR/.venv" --python python3 -q
  uv pip install --python "$DIR/.venv/bin/python" -q -r "$DIR/requirements.txt"
else
  python3 -m venv "$DIR/.venv"
  "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
fi
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/agtop" <<LAUNCHER
#!/bin/sh
exec "$DIR/.venv/bin/python" "$DIR/agtop.py" "\$@"
LAUNCHER
chmod +x "$HOME/.local/bin/agtop"
echo "agtop installed -> $HOME/.local/bin/agtop"
