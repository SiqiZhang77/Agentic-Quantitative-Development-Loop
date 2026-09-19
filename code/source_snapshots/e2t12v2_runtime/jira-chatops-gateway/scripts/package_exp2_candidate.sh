#!/usr/bin/env bash
set -euo pipefail

# Build a content-addressed deployment bundle for the isolated Experiment 2 DAG.
# Run only from a clean committed feature/exp2-rag-v1 worktree.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
output_dir="${1:-$repo_root/dist/exp2}"

if ! git -C "$repo_root" diff --quiet || ! git -C "$repo_root" diff --cached --quiet; then
  echo "ERROR: commit or stash tracked changes before packaging" >&2
  exit 1
fi

branch_name="$(git -C "$repo_root" branch --show-current)"
if [ "$branch_name" != "feature/exp2-rag-v1" ]; then
  echo "ERROR: expected feature/exp2-rag-v1, got $branch_name" >&2
  exit 1
fi

revision="$(git -C "$repo_root" rev-parse HEAD)"
short_revision="$(git -C "$repo_root" rev-parse --short=12 HEAD)"
staging_dir="$(mktemp -d "${TMPDIR:-/tmp}/exp2-si-bundle.XXXXXX")"
trap 'rm -rf "$staging_dir"' EXIT
bundle_root="$staging_dir/exp2-si-airflow-$short_revision"

install -d "$bundle_root/dags"
install -d "$bundle_root/scripts/exp2_si_runtime/dags/adf_utils"
install -d "$bundle_root/scripts/exp2_si_runtime/schemas"

install -m 644 \
  "$repo_root/experiments/exp2/deployment/jira_exp2_si_loader.py" \
  "$bundle_root/dags/jira_exp2_si_loader.py"
install -m 755 \
  "$repo_root/experiments/exp2/deployment/install_exp2_candidate.sh" \
  "$bundle_root/install_exp2_candidate.sh"

install -m 644 /dev/null "$bundle_root/scripts/exp2_si_runtime/__init__.py"
install -m 644 /dev/null "$bundle_root/scripts/exp2_si_runtime/dags/__init__.py"

for source_name in \
  docker_sandbox_runner.py \
  jira_command_validation.py \
  jira_exp2_si_runner.py \
  jira_quant_common.py \
  jira_rag_retriever.py \
  repository_catalog.py
do
  install -m 644 \
    "$repo_root/jira-chatops-gateway/dags/$source_name" \
    "$bundle_root/scripts/exp2_si_runtime/dags/$source_name"
done

install -m 644 \
  "$repo_root/jira-chatops-gateway/dags/adf_utils/__init__.py" \
  "$bundle_root/scripts/exp2_si_runtime/dags/adf_utils/__init__.py"
install -m 644 \
  "$repo_root/jira-chatops-gateway/dags/adf_utils/builder.py" \
  "$bundle_root/scripts/exp2_si_runtime/dags/adf_utils/builder.py"
install -m 644 \
  "$repo_root/jira-chatops-gateway/schemas/runtime_request.schema.json" \
  "$bundle_root/scripts/exp2_si_runtime/schemas/runtime_request.schema.json"
install -m 644 \
  "$repo_root/jira-chatops-gateway/schemas/runtime_response.schema.json" \
  "$bundle_root/scripts/exp2_si_runtime/schemas/runtime_response.schema.json"

printf '%s\n' "$revision" > \
  "$bundle_root/scripts/exp2_si_runtime/dags/.bslagenticquantdevloop_revision"

(
  cd "$bundle_root"
  find dags scripts -type f -print0 \
    | sort -z \
    | xargs -0 shasum -a 256 \
    > manifest.sha256
)

install -d "$output_dir"
archive="$output_dir/exp2-si-airflow-$short_revision.tar.gz"
tar -C "$staging_dir" -czf "$archive" "$(basename "$bundle_root")"
(
  cd "$output_dir"
  shasum -a 256 "$(basename "$archive")" > "$(basename "$archive").sha256"
)

echo "Bundle: $archive"
echo "Digest: $(cut -d ' ' -f 1 "$archive.sha256")"
echo "Revision: $revision"
