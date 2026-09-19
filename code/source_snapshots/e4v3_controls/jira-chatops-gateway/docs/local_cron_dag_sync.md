# Local Cron DAG Sync

## Goal

Use a lightweight cron job on the Airflow main node to sync DAG Python files,
runtime schemas, and the HDFS simulation-service bridge from GitHub into
Airflow's configured folders.

This implements Option 1: Local Cron / Systemd Git Poll.

## Source and target

Source path inside the GitHub repository:

`jira-chatops-gateway/dags/`

`jira-chatops-gateway/schemas/`

`jira-chatops-gateway/scripts/hdfs_simulation_service_bridge.py`

Target path on the Airflow main node:

`/opt/airflow3/dags`

`/opt/airflow3/schemas`

`/opt/airflow3/scripts/hdfs_simulation_service_bridge.py`

The live Airflow deployment already reads DAGs directly from `/opt/airflow3/dags`, so this implementation updates the existing DAG files in that folder rather than syncing into a separate subfolder.

## Sync script

The sync script is:

`jira-chatops-gateway/scripts/sync_airflow_dags_from_git.sh`

The script:

1. Reads GitHub credentials from an Airflow Connection.
2. Clones the configured GitHub repository if it does not already exist locally.
3. Fetches and resets to the selected branch if the repository already exists.
4. Copies DAG Python files and DAG-local helper packages from
   `jira-chatops-gateway/dags/` into `/opt/airflow3/dags`.
5. Copies runtime JSON schemas from `jira-chatops-gateway/schemas/` into
   `/opt/airflow3/schemas`, matching the DAG validators' lookup path.
6. Atomically installs only `hdfs_simulation_service_bridge.py` into
   `/opt/airflow3/scripts`; it does not overwrite the running sync script.
7. Does not use `rsync --delete`, so unrelated files, backup files, folders, and manually deployed DAGs are not deleted.
8. Avoids preserving destination ownership, group, and permissions so it works safely with the existing Airflow folder permissions.
9. Uses a lock file so two syncs do not run at the same time.
10. Avoids printing the GitHub token to logs.

Once the updated script has been installed at
`/opt/airflow3/scripts/sync_airflow_dags_from_git.sh`, the regular cron run
keeps DAGs, schemas, and the bridge current. If this sync script itself changes in Git,
run the script once to refresh `/opt/airflow3/dag-sync/repo`, then copy the
new script from the refreshed worktree into `/opt/airflow3/scripts/`.

## Required environment variables

The cron job must provide:

```text
REPO_URL
AIRFLOW_DAGS_FOLDER
```

## GitHub credentials

Store the GitHub token in an Airflow Connection, normally `github_default`.

Connection fields:

```text
Connection ID: github_default
Login:         GitHub username or bot name
Password:      GitHub token
```

To use a different connection for DAG sync, set
`DAG_SYNC_GITHUB_CONNECTION_ID` in the cron environment or as an Airflow
Variable. If unset, the script checks `GITHUB_CONNECTION_ID` and then falls back
to `github_default`.

The old `GITHUB_USERNAME` and `GITHUB_TOKEN` Airflow Variables remain as a
temporary compatibility fallback, but new deployments should not use them.

## Optional environment variables

```text
BRANCH                         defaults to main
WORKTREE_DIR                   defaults to /opt/airflow3/dag-sync/repo
AIRFLOW_HOME                   defaults to /opt/airflow3
AIRFLOW_PYTHON                 defaults to /opt/airflow3/venv/bin/python
AIRFLOW_SCHEMAS_FOLDER         defaults to $AIRFLOW_HOME/schemas
AIRFLOW_SCRIPTS_FOLDER         defaults to $AIRFLOW_HOME/scripts
DAG_SYNC_GITHUB_CONNECTION_ID  Airflow Connection ID for cloning this repo
```

## Manual cluster sync

Log in to the Airflow main node as the Airflow user, then run:

```bash
REPO_URL="https://github.com/bankingscience/BSLAgenticQuantDevLoop.git" \
BRANCH="main" \
WORKTREE_DIR="/opt/airflow3/dag-sync/repo" \
AIRFLOW_DAGS_FOLDER="/opt/airflow3/dags" \
AIRFLOW_SCHEMAS_FOLDER="/opt/airflow3/schemas" \
AIRFLOW_SCRIPTS_FOLDER="/opt/airflow3/scripts" \
AIRFLOW_HOME="/opt/airflow3" \
AIRFLOW_PYTHON="/opt/airflow3/venv/bin/python" \
bash /opt/airflow3/scripts/sync_airflow_dags_from_git.sh
```

Check the sync log and verify the DAG-local helper package is available:

```bash
tail -100 /opt/airflow3/logs/airflow-dag-sync.log
ls -la /opt/airflow3/dags/adf_utils
ls -la /opt/airflow3/schemas
ls -la /opt/airflow3/scripts/hdfs_simulation_service_bridge.py
/opt/airflow3/venv/bin/python -c "from adf_utils.builder import build_runtime_workflow_report_adf; print('ok')"
```

If the sync script itself changed, refresh the installed copy after the manual
sync:

```bash
cp /opt/airflow3/dag-sync/repo/jira-chatops-gateway/scripts/sync_airflow_dags_from_git.sh /opt/airflow3/scripts/sync_airflow_dags_from_git.sh
chmod +x /opt/airflow3/scripts/sync_airflow_dags_from_git.sh
```

## Cron example

Install the same environment in the Airflow user's crontab so the cluster keeps
the DAGs and schemas current without interactive Git credentials:

```cron
*/5 * * * * REPO_URL="https://github.com/bankingscience/BSLAgenticQuantDevLoop.git" BRANCH="main" WORKTREE_DIR="/opt/airflow3/dag-sync/repo" AIRFLOW_DAGS_FOLDER="/opt/airflow3/dags" AIRFLOW_SCHEMAS_FOLDER="/opt/airflow3/schemas" AIRFLOW_SCRIPTS_FOLDER="/opt/airflow3/scripts" AIRFLOW_HOME="/opt/airflow3" AIRFLOW_PYTHON="/opt/airflow3/venv/bin/python" bash /opt/airflow3/scripts/sync_airflow_dags_from_git.sh >> /opt/airflow3/logs/airflow-dag-sync.log 2>&1
```

## Promote latest sandbox image to stable

The Airflow DAGs default to the `:stable` sandbox image. To promote the current
`:latest` image to `:stable`, run the existing GitHub Actions workflow:

1. Open GitHub Actions for this repository.
2. Select **Promote to Stable**.
3. Select **Run workflow**.
4. Leave `source_tag` as `latest`, unless promoting a specific image tag.
5. Run the workflow and wait for it to complete successfully.

The workflow is defined in `.github/workflows/promote-stable.yml`. It pulls
`ghcr.io/bankingscience/bslagenticquantdevloop/sandbox:<source_tag>`, retags it
as `:stable`, pushes `:stable`, and prints the promoted digest.
