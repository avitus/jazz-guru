#!/usr/bin/env bash
# jazz-guru local-dev bootstrap (no Docker).
# Installs Postgres+pgvector, Redis, and FluidSynth via Homebrew on macOS;
# creates the database and the pgvector extension; runs alembic migrations.
set -euo pipefail

DB_NAME="${DB_NAME:-jazz_guru}"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m!\033[0m %s\n' "$*"; }
die()   { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# --- platform check -----------------------------------------------------------
case "$(uname -s)" in
  Darwin) PLATFORM=mac ;;
  Linux)  PLATFORM=linux ;;
  *) die "unsupported platform: $(uname -s)" ;;
esac

# --- Homebrew packages (macOS) ------------------------------------------------
if [[ "$PLATFORM" == "mac" ]]; then
  command -v brew >/dev/null 2>&1 || die "Homebrew not installed. See https://brew.sh"
  bold "Installing system deps via Homebrew"
  brew list --versions redis       >/dev/null 2>&1 || brew install redis
  brew list --versions fluid-synth >/dev/null 2>&1 || brew install fluid-synth
  brew list --versions ffmpeg      >/dev/null 2>&1 || brew install ffmpeg
  # xz provides liblzma; CPython needs it at build time or _lzma is omitted,
  # which breaks any transitive `import lzma` (librosa→joblib/pooch hit this).
  brew list --versions xz          >/dev/null 2>&1 || brew install xz

  # Detect a running/installed Homebrew Postgres. Reuse it if present, else install @16.
  PG_FORMULA=""
  for f in postgresql@17 postgresql@16 postgresql@15 postgresql@14 postgresql; do
    if brew list --versions "$f" >/dev/null 2>&1; then
      PG_FORMULA="$f"
      break
    fi
  done
  if [[ -z "$PG_FORMULA" ]]; then
    bold "Installing postgresql@16"
    brew install postgresql@16
    PG_FORMULA="postgresql@16"
  else
    ok "reusing existing $PG_FORMULA"
  fi

  # Self-healing service start. `brew services start <svc>` can fail
  # with `Bootstrap failed: 5: Input/output error` for several reasons.
  # The most common one in practice: another process is already bound
  # to the service's port (e.g. a manual `redis-server` left behind
  # from a previous unblock attempt). launchd spawns its own copy, the
  # spawn exits immediately because the port is taken, and bootstrap
  # reports failure — followed by an indefinite spawn-retry loop
  # visible in `log show --predicate 'process=="launchd"'`.
  # Less common: stale registration in gui/<uid>, the service disabled
  # in user-launchd state, or a corrupt plist that bootout won't fix.
  #
  # Strategy:
  #   0. If the service's port is already listening, declare victory.
  #      The agent only cares that something is answering on the right
  #      socket — not whether launchd is the parent.
  #   1. brew services start.
  #   2. bootout + enable + retry.
  #   3. delete the user-level plist + retry.
  #   4. spawn the daemon directly (only when caller provided a
  #      fallback command + probe).
  start_service() {
    local svc="$1"
    local cmd_fallback="${2:-}"
    local probe="${3:-}"

    # Already up? Done.
    if [[ -n "$probe" ]] && eval "$probe" >/dev/null 2>&1; then
      ok "$svc already listening"
      return 0
    fi

    if brew services start "$svc" >/dev/null 2>&1; then
      return 0
    fi

    warn "brew services start $svc failed; clearing stale launchd state"
    launchctl bootout "gui/$(id -u)/homebrew.mxcl.$svc" >/dev/null 2>&1 || true
    launchctl enable  "gui/$(id -u)/homebrew.mxcl.$svc" >/dev/null 2>&1 || true
    if brew services start "$svc" >/dev/null 2>&1; then
      return 0
    fi

    # Some installs end up with a plist launchd won't accept; remove
    # it so the next brew services start regenerates from the formula.
    if [[ -f "$HOME/Library/LaunchAgents/homebrew.mxcl.$svc.plist" ]]; then
      warn "removing $HOME/Library/LaunchAgents/homebrew.mxcl.$svc.plist and retrying"
      rm -f "$HOME/Library/LaunchAgents/homebrew.mxcl.$svc.plist"
      if brew services start "$svc" >/dev/null 2>&1; then
        return 0
      fi
    fi

    # Last resort: run the daemon directly, bypassing launchd. Only
    # applies when the caller provided a fallback command + probe.
    if [[ -n "$cmd_fallback" && -n "$probe" ]]; then
      warn "launchd will not bootstrap $svc; starting daemon directly"
      eval "$cmd_fallback"
      sleep 1
      eval "$probe" >/dev/null 2>&1 || die "$svc failed to come up under the fallback launcher"
      ok "$svc running (not under launchd)"
      return 0
    fi
    die "could not start $svc (brew services exited non-zero and no fallback configured)"
  }

  bold "Starting services"
  start_service "$PG_FORMULA"
  # Redis fallback: spawn redis-server detached on 127.0.0.1:6379 with
  # the daemonize flag. Matches the local-dev assumption (loopback only).
  REDIS_BIN="$(brew --prefix redis 2>/dev/null)/bin/redis-server"
  start_service redis \
    "nohup '$REDIS_BIN' --daemonize yes --port 6379 --bind 127.0.0.1 >/dev/null 2>&1" \
    "lsof -nP -iTCP:6379 -sTCP:LISTEN"
  ok "$PG_FORMULA + redis running"

  PG_PREFIX="$(brew --prefix "$PG_FORMULA")"
  PG_BIN="$PG_PREFIX/bin"
  export PATH="$PG_BIN:$PATH"
  PG_CONFIG="$PG_BIN/pg_config"

  # --- pgvector: install/build for the *running* Postgres version -------------
  PG_VERSION="$("$PG_CONFIG" --version | awk '{print $2}' | cut -d. -f1)"
  PG_SHARE="$("$PG_CONFIG" --sharedir)"
  if [[ -f "$PG_SHARE/extension/vector.control" ]]; then
    ok "pgvector already installed for Postgres $PG_VERSION"
  else
    bold "Installing pgvector for Postgres $PG_VERSION"
    # try the Homebrew formula first (works when it matches the active PG version)
    brew list --versions pgvector >/dev/null 2>&1 || brew install pgvector || true
    if [[ ! -f "$PG_SHARE/extension/vector.control" ]]; then
      warn "Homebrew pgvector does not target Postgres $PG_VERSION; building from source"
      tmp="$(mktemp -d)"
      git clone --depth 1 --branch v0.8.0 https://github.com/pgvector/pgvector.git "$tmp/pgvector"
      ( cd "$tmp/pgvector" && PG_CONFIG="$PG_CONFIG" make && PG_CONFIG="$PG_CONFIG" make install )
      ok "pgvector built and installed for Postgres $PG_VERSION"
    fi
  fi
fi

# --- database -----------------------------------------------------------------
bold "Provisioning database '$DB_NAME'"
if psql -lqt | cut -d '|' -f 1 | grep -qw "$DB_NAME"; then
  ok "database $DB_NAME already exists"
else
  createdb "$DB_NAME"
  ok "created database $DB_NAME"
fi

psql "$DB_NAME" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null
ok "pgvector extension ensured"

# --- Python venv --------------------------------------------------------------
if [[ ! -d .venv ]]; then
  bold "Creating Python venv at .venv"
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
bold "Installing project (.venv)"
.venv/bin/python -m pip install -q -e ".[dev]"
ok "python deps installed"

# Verify the venv's Python has _lzma. Pyenv-built CPython skips this C
# extension silently when xz headers were missing at build time; the
# failure only surfaces later when librosa pulls in joblib/pooch.
if ! .venv/bin/python -c "import lzma" >/dev/null 2>&1; then
  PYBIN="$(.venv/bin/python -c 'import sys; print(sys.executable)')"
  PYBASE="$(.venv/bin/python -c 'import sys; print(sys.base_prefix)')"
  case "$PLATFORM" in
    mac)   XZ_INSTALL="brew install xz" ;;
    linux)
      if   command -v apt-get >/dev/null 2>&1; then XZ_INSTALL="sudo apt-get install -y xz-utils  # Debian/Ubuntu (package is xz-utils, not xz)"
      elif command -v dnf     >/dev/null 2>&1; then XZ_INSTALL="sudo dnf install -y xz            # Fedora/RHEL"
      elif command -v yum     >/dev/null 2>&1; then XZ_INSTALL="sudo yum install -y xz            # older RHEL/CentOS"
      elif command -v pacman  >/dev/null 2>&1; then XZ_INSTALL="sudo pacman -S --noconfirm xz    # Arch"
      else XZ_INSTALL="install your distro's xz / liblzma development headers"
      fi
      ;;
    *) XZ_INSTALL="install your platform's xz / liblzma development package" ;;
  esac
  die "venv python ($PYBIN, built from $PYBASE) is missing the _lzma stdlib extension.
   Rebuild the underlying CPython with xz available, then recreate the venv:
     $XZ_INSTALL
     pyenv uninstall \$(basename \"$PYBASE\")   # if pyenv-managed
     pyenv install   \$(basename \"$PYBASE\")
     rm -rf .venv && make install"
fi
ok "_lzma extension present"

# --- .env ---------------------------------------------------------------------
if [[ ! -f .env ]]; then
  cp .env.example .env
  warn "created .env from .env.example — edit it to add your ANTHROPIC_API_KEY"
fi

# --- audio engines + sample libraries -----------------------------------------
# Install/build the offline renderers and the SFZ libraries referenced by
# data/instruments.yaml. All steps are idempotent — a second `make setup`
# is a no-op once everything is in place.
INSTR_ROOT="${JG_INSTRUMENTS_ROOT:-$HOME/.local/share/jazz-guru/instruments}"
SF_PATH="$INSTR_ROOT/soundfonts/FluidR3Mono_GM.sf3"
LOCAL_BIN="$HOME/.local/bin"
SFIZZ_BIN="$LOCAL_BIN/sfizz_render"

mkdir -p "$INSTR_ROOT/soundfonts" "$LOCAL_BIN"

# 1. sfizz_render — builds from source. Not in Homebrew (no formula, no tap).
if [[ -x "$SFIZZ_BIN" ]]; then
  ok "sfizz_render already installed at $SFIZZ_BIN"
elif [[ "$PLATFORM" == "mac" ]]; then
  bold "Building sfizz_render from source (sfztools/sfizz @ 1.2.3)"
  brew list --versions cmake >/dev/null 2>&1 || brew install cmake
  tmp_sfizz="$(mktemp -d)"
  trap 'rm -rf "$tmp_sfizz"' EXIT
  git clone --depth 1 --branch 1.2.3 --recurse-submodules --shallow-submodules \
    https://github.com/sfztools/sfizz.git "$tmp_sfizz/sfizz" >/dev/null 2>&1
  cd "$tmp_sfizz/sfizz"

  # Patch 1: SfizzConfig.cmake's ARM gate matches "arm64" and unconditionally
  # adds -mfpu=neon -mfloat-abi=hard, which Clang on arm64-darwin rejects.
  # Tighten the regex so arm64 / aarch64 don't fall into the 32-bit-ARM branch.
  python3 - <<'PY'
import pathlib
p = pathlib.Path("cmake/SfizzConfig.cmake")
s = p.read_text()
needle = 'elseif(PROJECT_SYSTEM_PROCESSOR MATCHES "(arm.*)")'
repl = 'elseif(PROJECT_SYSTEM_PROCESSOR MATCHES "^arm" AND NOT PROJECT_SYSTEM_PROCESSOR MATCHES "^arm64" AND NOT PROJECT_SYSTEM_PROCESSOR MATCHES "^aarch64")'
if needle in s:
    p.write_text(s.replace(needle, repl))
PY

  # Patch 2: atomic_queue's `Base::template do_pop_any(...)` is rejected by
  # Clang 16+ which requires an explicit `<>` after `template`-prefixed names.
  python3 - <<'PY'
import pathlib
p = pathlib.Path("external/atomic_queue/include/atomic_queue/atomic_queue.h")
s = p.read_text()
for n, r in [
    ("Base::template do_pop_any(states_[index]",  "Base::template do_pop_any<>(states_[index]"),
    ("Base::template do_push_any(std::forward<U>(element)", "Base::template do_push_any<>(std::forward<U>(element)"),
]:
    if n in s:
        s = s.replace(n, r)
p.write_text(s)
PY

  cmake -B build -DCMAKE_BUILD_TYPE=Release \
    -DSFIZZ_RENDER=ON -DSFIZZ_JACK=OFF -DSFIZZ_TESTS=OFF \
    -DSFIZZ_DEMOS=OFF -DSFIZZ_BENCHMARKS=OFF >/dev/null
  cmake --build build --target sfizz_render -j "$(sysctl -n hw.ncpu)" >/dev/null
  cp build/library/bin/sfizz_render "$SFIZZ_BIN"
  chmod +x "$SFIZZ_BIN"
  cd - >/dev/null
  rm -rf "$tmp_sfizz"
  trap - EXIT
  ok "sfizz_render installed at $SFIZZ_BIN"
  if ! printf '%s\n' "$PATH" | tr ':' '\n' | grep -qx "$LOCAL_BIN"; then
    warn "$LOCAL_BIN is not on \$PATH — add it to your shell rc so jazz-guru can find sfizz_render"
  fi
else
  warn "sfizz_render install only automated on macOS; build from source per https://github.com/sfztools/sfizz"
fi

# 2. SFZ sample libraries — paths must match data/instruments.yaml.
bold "Cloning SFZ libraries into $INSTR_ROOT (~1.1 GB total; first run only)"
clone_lib() {
  local repo="$1" dst="$2" size="$3"
  if [[ -d "$INSTR_ROOT/$dst/.git" ]]; then
    ok "$dst already present"
  else
    warn "cloning $repo (~$size) -> $dst"
    git clone --depth 1 "https://github.com/sfzinstruments/$repo.git" "$INSTR_ROOT/$dst"
  fi
}
clone_lib MTG.SoloSax         mtg-solo-sax 220MB
clone_lib SalamanderGrandPiano salamander   1.4GB
clone_lib dsmolken.double-bass smolken-bass 533MB

# 3. FluidR3 GM soundfont — MuseScore's mirror is the most reliable host.
if [[ -f "$SF_PATH" ]]; then
  ok "soundfont already present at $SF_PATH"
else
  bold "Downloading FluidR3Mono_GM.sf3 (~14 MB) from MuseScore mirror"
  curl -fsSL --retry 3 -o "$SF_PATH.tmp" \
    "https://github.com/musescore/MuseScore/raw/2.1/share/sound/FluidR3Mono_GM.sf3"
  mv "$SF_PATH.tmp" "$SF_PATH"
  ok "soundfont installed at $SF_PATH"
fi

# 4. Wire FLUIDSYNTH_SOUNDFONT + JG_INSTRUMENTS_ROOT in .env if they are blank.
# Pydantic-settings treats a `FOO=` env line as the literal empty string, which
# overrides the Python default — so blank values cause real bugs (e.g.
# `_resolve_library` falls through to CWD). Always emit the concrete path.
awk -v sf="$SF_PATH" -v root="$INSTR_ROOT" '
  BEGIN{FS=OFS="="}
  /^FLUIDSYNTH_SOUNDFONT=$/{print "FLUIDSYNTH_SOUNDFONT="sf; next}
  /^JG_INSTRUMENTS_ROOT=$/{print "JG_INSTRUMENTS_ROOT="root; next}
  {print}
' .env > .env.tmp && mv .env.tmp .env
ok ".env wired (FLUIDSYNTH_SOUNDFONT, JG_INSTRUMENTS_ROOT)"

# --- alembic ------------------------------------------------------------------
bold "Running alembic migrations"
.venv/bin/alembic upgrade head
ok "schema up to date"

bold "Verifying"
.venv/bin/jazz-guru info | head -20 || true

cat <<EOF

$(bold "Done.")
Next:
  source .venv/bin/activate          # optional: activate the venv
  jazz-guru ping                     # test Anthropic connectivity
  jazz-guru chat "compose a Cmaj7 arpeggio as out/arp.mid"
  jazz-guru-server                   # FastAPI on http://127.0.0.1:8000
  jazz-guru-worker                   # background distillation/eval worker (needs redis)
EOF
