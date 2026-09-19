#!/usr/bin/env bash
set -euo pipefail

# Main-node Airflow DAG sync script.
#
# Intended use:
#   Run this script from cron on the Airflow main node.
#
# What it does:
#   1. Reads GitHub credentials from an Airflow Connection.
#   2. Clones or updates the configured GitHub repository.
#   3. Takes DAG files from jira-chatops-gateway/dags/.
#   4. Copies DAG Python files into Airflow's dags_folder.
#   5. Copies helper packages located under the DAG source directory.
#   6. Copies runtime schema JSON files to Airflow's schemas folder.
#   7. Atomically installs the HDFS simulation-service bridge runtime script.
#
# Important:
#   This script does NOT use rsync --delete.
#   It updates/copies required Python files and packages, but does not delete
#   unrelated files, backup files, folders, or manually deployed DAGs in
#   /opt/airflow3/dags.

: "${REPO_URL:?REPO_URL is required}"
: "${AIRFLOW_DAGS_FOLDER:?AIRFLOW_DAGS_FOLDER is required}"

: "${BRANCH:=main}"
: "${WORKTREE_DIR:=/opt/airflow3/dag-sync/repo}"
: "${AIRFLOW_HOME:=/opt/airflow3}"
: "${AIRFLOW_PYTHON:=/opt/airflow3/venv/bin/python}"

SRC_SUBDIR="jira-chatops-gateway/dags"
SCHEMA_SRC_SUBDIR="jira-chatops-gateway/schemas"
RUNTIME_SCRIPT_SRC_SUBDIR="jira-chatops-gateway/scripts"
BRIDGE_SCRIPT_NAME="hdfs_simulation_service_bridge.py"
AIRFLOW_SCHEMAS_FOLDER="${AIRFLOW_SCHEMAS_FOLDER:-$AIRFLOW_HOME/schemas}"
AIRFLOW_SCRIPTS_FOLDER="${AIRFLOW_SCRIPTS_FOLDER:-$AIRFLOW_HOME/scripts}"
LOCK_FILE="/tmp/airflow-dag-sync.lock"

echo "Starting Airflow DAG sync at $(date -Is)"
echo "Repo URL: $REPO_URL"
echo "Branch: $BRANCH"
echo "Worktree dir: $WORKTREE_DIR"
echo "Airflow DAG target folder: $AIRFLOW_DAGS_FOLDER"
echo "Airflow schema target folder: $AIRFLOW_SCHEMAS_FOLDER"
echo "Airflow runtime script target folder: $AIRFLOW_SCRIPTS_FOLDER"

mkdir -p "$(dirname "$WORKTREE_DIR")"

if [ ! -d "$AIRFLOW_DAGS_FOLDER" ]; then
  echo "ERROR: Airflow DAG folder does not exist: $AIRFLOW_DAGS_FOLDER" >&2
  echo "Refusing to create it automatically. Confirm the correct Airflow dags_folder path." >&2
  exit 1
fi

if [ ! -x "$AIRFLOW_PYTHON" ]; then
  echo "ERROR: Airflow Python not found or not executable: $AIRFLOW_PYTHON" >&2
  exit 1
fi

if [ ! -d "$AIRFLOW_SCRIPTS_FOLDER" ]; then
  echo "ERROR: Airflow scripts folder does not exist: $AIRFLOW_SCRIPTS_FOLDER" >&2
  echo "Refusing to create it automatically. Confirm the correct operational scripts path." >&2
  exit 1
fi

# Read GitHub credentials without printing the token.
#
# Preferred:
#   Airflow Connection DAG_SYNC_GITHUB_CONNECTION_ID, or GITHUB_CONNECTION_ID,
#   falling back to github_default.
#
# Temporary compatibility fallback:
#   Airflow Variables GITHUB_USERNAME and GITHUB_TOKEN.
#
# tail -n 1 is used because Airflow startup may emit log lines before printing
# the actual JSON value.
GITHUB_CREDENTIALS_JSON="$(
  AIRFLOW_HOME="$AIRFLOW_HOME" "$AIRFLOW_PYTHON" - <<'PY' | tail -n 1
import json
import os

try:
    from airflow.sdk import BaseHook, Variable
except ImportError:
    from airflow.hooks.base import BaseHook
    from airflow.models import Variable


def get_variable(name, default=None):
    try:
        return Variable.get(name, default_var=default)
    except TypeError:
        return Variable.get(name, default)
    except Exception:
        return default


def get_connection(conn_id):
    try:
        return BaseHook.get_connection(conn_id)
    except Exception:
        try:
            from airflow.models.connection import Connection
            from airflow.settings import Session

            session = Session()
            try:
                conn = (
                    session.query(Connection)
                    .filter(Connection.conn_id == conn_id)
                    .one_or_none()
                )
                if conn is None:
                    return None
                session.expunge(conn)
                return conn
            finally:
                session.close()
        except Exception:
            return None


connection_id = (
    os.environ.get("DAG_SYNC_GITHUB_CONNECTION_ID")
    or get_variable("DAG_SYNC_GITHUB_CONNECTION_ID")
    or get_variable("GITHUB_CONNECTION_ID")
    or "github_default"
)

username = ""
token = ""
source = ""

conn = get_connection(connection_id) if connection_id else None
if conn is not None:
    extra = getattr(conn, "extra_dejson", {}) or {}
    username = conn.login or extra.get("username") or "x-access-token"
    token = conn.password or extra.get("token") or extra.get("api_key") or ""
    source = f"Airflow Connection {connection_id}"

if not token:
    username = get_variable("GITHUB_USERNAME", "x-access-token")
    token = get_variable("GITHUB_TOKEN", "")
    source = "legacy Airflow Variables GITHUB_USERNAME/GITHUB_TOKEN"

print(
    json.dumps(
        {
            "username": username or "x-access-token",
            "token": token or "",
            "source": source,
        }
    )
)
PY
)"

GITHUB_USERNAME_VALUE="$(
  CREDENTIALS_JSON="$GITHUB_CREDENTIALS_JSON" "$AIRFLOW_PYTHON" - <<'PY'
import json
import os

print(json.loads(os.environ["CREDENTIALS_JSON"])["username"])
PY
)"

GITHUB_TOKEN_VALUE="$(
  CREDENTIALS_JSON="$GITHUB_CREDENTIALS_JSON" "$AIRFLOW_PYTHON" - <<'PY'
import json
import os

print(json.loads(os.environ["CREDENTIALS_JSON"])["token"])
PY
)"

GITHUB_CREDENTIAL_SOURCE="$(
  CREDENTIALS_JSON="$GITHUB_CREDENTIALS_JSON" "$AIRFLOW_PYTHON" - <<'PY'
import json
import os

print(json.loads(os.environ["CREDENTIALS_JSON"])["source"])
PY
)"

if [ -z "$GITHUB_TOKEN_VALUE" ]; then
  echo "ERROR: GitHub token is missing. Configure Airflow Connection github_default or set DAG_SYNC_GITHUB_CONNECTION_ID/GITHUB_CONNECTION_ID to a connection with the token in Password." >&2
  exit 1
fi

echo "GitHub credentials source: $GITHUB_CREDENTIAL_SOURCE"
if [ "$GITHUB_CREDENTIAL_SOURCE" = "legacy Airflow Variables GITHUB_USERNAME/GITHUB_TOKEN" ]; then
  echo "WARNING: GitHub sync credentials came from Airflow Variables. Move the token to an Airflow Connection." >&2
fi

# Use GIT_ASKPASS so the token is not embedded directly in REPO_URL.
ASKPASS_FILE="$(mktemp)"
chmod 700 "$ASKPASS_FILE"

cat > "$ASKPASS_FILE" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  *Username*) printf "%s\n" "$GITHUB_USERNAME_VALUE" ;;
  *Password*) printf "%s\n" "$GITHUB_TOKEN_VALUE" ;;
  *) printf "\n" ;;
esac
EOF

cleanup() {
  rm -f "$ASKPASS_FILE"
}
trap cleanup EXIT

export GITHUB_USERNAME_VALUE
export GITHUB_TOKEN_VALUE
export GIT_ASKPASS="$ASKPASS_FILE"
export GIT_TERMINAL_PROMPT=0

(
  flock -n 9 || {
    echo "Another DAG sync is already running. Exiting."
    exit 0
  }

  if [ ! -d "$WORKTREE_DIR/.git" ]; then
    echo "Cloning branch $BRANCH into $WORKTREE_DIR"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$WORKTREE_DIR"
  else
    echo "Updating existing clone in $WORKTREE_DIR"
    git -C "$WORKTREE_DIR" fetch --depth 1 origin "$BRANCH"
    git -C "$WORKTREE_DIR" reset --hard "FETCH_HEAD"
  fi

  if [ ! -d "$WORKTREE_DIR/$SRC_SUBDIR" ]; then
    echo "ERROR: Expected DAG source directory not found: $WORKTREE_DIR/$SRC_SUBDIR" >&2
    exit 1
  fi

  if [ ! -d "$WORKTREE_DIR/$SCHEMA_SRC_SUBDIR" ]; then
    echo "ERROR: Expected schema source directory not found: $WORKTREE_DIR/$SCHEMA_SRC_SUBDIR" >&2
    exit 1
  fi

  if [ ! -f "$WORKTREE_DIR/$RUNTIME_SCRIPT_SRC_SUBDIR/$BRIDGE_SCRIPT_NAME" ]; then
    echo "ERROR: Expected bridge script not found: $WORKTREE_DIR/$RUNTIME_SCRIPT_SRC_SUBDIR/$BRIDGE_SCRIPT_NAME" >&2
    exit 1
  fi

  SOURCE_REVISION="$(git -C "$WORKTREE_DIR" rev-parse HEAD)"
  echo "Source revision: $SOURCE_REVISION"

  echo "Copying DAG Python files from $WORKTREE_DIR/$SRC_SUBDIR/ to $AIRFLOW_DAGS_FOLDER/"

  # Copy/update top-level DAG .py files and helper packages.
  # Do not delete unrelated files in the Airflow DAG folder.
  # Do not preserve owner/group/perms because /opt/airflow3/dags has existing shared permissions.
  rsync -rltO --no-perms --no-owner --no-group \
    --include="*.py" \
    --include="adf_utils/***" \
    --exclude="*" \
    "$WORKTREE_DIR/$SRC_SUBDIR/" \
    "$AIRFLOW_DAGS_FOLDER/"
  printf '%s\n' "$SOURCE_REVISION" > "$AIRFLOW_DAGS_FOLDER/.bslagenticquantdevloop_revision"

  echo "Copying runtime schema JSON files from $WORKTREE_DIR/$SCHEMA_SRC_SUBDIR/ to $AIRFLOW_SCHEMAS_FOLDER/"

  mkdir -p "$AIRFLOW_SCHEMAS_FOLDER"

  # Keep runtime schemas deployed with DAG code because validators load them
  # relative to AIRFLOW_HOME, not from the DAG source directory.
  rsync -rltO --no-perms --no-owner --no-group \
    --include="*.json" \
    --exclude="*" \
    "$WORKTREE_DIR/$SCHEMA_SRC_SUBDIR/" \
    "$AIRFLOW_SCHEMAS_FOLDER/"

  # Install only the bridge runtime script. Do not overwrite this sync script
  # while it is executing. A same-directory rename makes deployment atomic.
  BRIDGE_INSTALL_TMP="$AIRFLOW_SCRIPTS_FOLDER/.$BRIDGE_SCRIPT_NAME.tmp.$$"
  install -m 0775 \
    "$WORKTREE_DIR/$RUNTIME_SCRIPT_SRC_SUBDIR/$BRIDGE_SCRIPT_NAME" \
    "$BRIDGE_INSTALL_TMP"
  mv -f "$BRIDGE_INSTALL_TMP" "$AIRFLOW_SCRIPTS_FOLDER/$BRIDGE_SCRIPT_NAME"
  echo "Installed runtime bridge: $AIRFLOW_SCRIPTS_FOLDER/$BRIDGE_SCRIPT_NAME"

  echo "Synced DAG files from source:"
  find "$WORKTREE_DIR/$SRC_SUBDIR" -maxdepth 1 -type f -name "*.py" -printf "  %f\n" | sort
  echo "Synced schema files from source:"
  find "$WORKTREE_DIR/$SCHEMA_SRC_SUBDIR" -maxdepth 1 -type f -name "*.json" -printf "  %f\n" | sort

  echo "Airflow DAG sync complete at $(date -Is)"
) 9>"$LOCK_FILE"
