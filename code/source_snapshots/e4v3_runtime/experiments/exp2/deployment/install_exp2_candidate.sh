#!/usr/bin/env bash
set -euo pipefail

# Install only the isolated Experiment 2 loader and private runtime package.
# No production DAG, schema, script, Airflow Variable, or Connection is changed.

bundle_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
airflow_home="${AIRFLOW_HOME:-/opt/airflow3}"
airflow_dags_folder="${AIRFLOW_DAGS_FOLDER:-$airflow_home/dags}"
airflow_scripts_folder="${AIRFLOW_SCRIPTS_FOLDER:-$airflow_home/scripts}"
runtime_target="$airflow_scripts_folder/exp2_si_runtime"
loader_target="$airflow_dags_folder/jira_exp2_si_loader.py"

test -d "$airflow_dags_folder"
test -d "$airflow_scripts_folder"
test -f "$bundle_root/manifest.sha256"

(
  cd "$bundle_root"
  sha256sum -c manifest.sha256
)

install -d -m 750 "$runtime_target"
while IFS= read -r source_file; do
  relative_path="${source_file#"$bundle_root/scripts/exp2_si_runtime/"}"
  target_file="$runtime_target/$relative_path"
  install -d -m 750 "$(dirname "$target_file")"
  install -m 640 "$source_file" "$target_file"
done < <(find "$bundle_root/scripts/exp2_si_runtime" -type f | sort)

install -m 640 "$bundle_root/dags/jira_exp2_si_loader.py" "$loader_target"

echo "Installed isolated runtime: $runtime_target"
echo "Installed candidate loader: $loader_target"
echo "Production DAG files were not modified."
