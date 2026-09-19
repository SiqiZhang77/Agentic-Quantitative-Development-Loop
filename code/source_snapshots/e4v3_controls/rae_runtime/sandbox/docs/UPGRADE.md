# Upgrading a pinned dependency (rae_runtime/sandbox)

Single source of truth: `requirements.in` (direct deps only) is compiled to
the hash-pinned `requirements.txt`. The Dockerfile installs ONLY from
`requirements.txt` with `--no-deps --require-hashes`. Never hand-edit
`requirements.txt`, and never add a dep straight to it - go through the .in.

## Steps

1. Edit `requirements.in`: change/add the direct dep and its version.

2. Re-lock in a clean Python 3.12 container (pip-tools never enters the image):

       docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c \
         "pip install --no-cache-dir pip-tools && \
          pip-compile --generate-hashes --output-file=requirements.txt requirements.in"

   Note: drop `working-set.txt` from the command - it was a one-time seed for
   the first lock. The existing requirements.txt is now the baseline.

3. Rebuild and verify reproducibility (a clean rebuild must resolve the
   identical set):

       docker build -f sandbox/Dockerfile -t rae-local .
       docker build --no-cache -f sandbox/Dockerfile -t rae-b .
       docker run --rm --entrypoint python rae-local -m pip freeze | sort > a.txt
       docker run --rm --entrypoint python rae-b     -m pip freeze | sort > b.txt
       diff a.txt b.txt          # must be empty

4. Confirm the hardened invariants still hold:

       docker run --rm --entrypoint sh rae-local -c 'pip-compile --version; uv --version; gcc --version'  # all "not found"
       docker run --rm --entrypoint id rae-local      # uid=1000, non-root

5. Re-run the off-network e2e and confirm it still succeeds:

       mkdir -p /tmp/rae/output && chmod 777 /tmp/rae/output
       docker run --rm --network none \
         --user 1000:1000 --read-only \
         --tmpfs /tmp --tmpfs /workspace:uid=1000,gid=1000,mode=0750 \
         -v /tmp/rae/output:/workspace/output \
         --cap-drop ALL --security-opt no-new-privileges:true \
         --pids-limit 256 --memory 512m \
         -e RAE_OFFLINE=1 -e TICKET="TEST-1" -e REQUEST="add an RSI filter" \
         -e RESULT_PATH="/workspace/output/result.json" \
         rae-local
       cat /tmp/rae/output/result.json   # status: succeeded

6. Push and record the NEW registry manifest digest:

       docker tag rae-local ghcr.io/bankingscience/bslagenticquantdevloop/sandbox:latest
       docker push          ghcr.io/bankingscience/bslagenticquantdevloop/sandbox:latest
       docker inspect --format='{{range .RepoDigests}}{{println .}}{{end}}' \
         ghcr.io/bankingscience/bslagenticquantdevloop/sandbox:latest
       # copy the ghcr.io/... line; hand the new sha256 to RAE-01 / the DockerOperator

   Pushing needs a credential with `write:packages`. Per RAE-01, prefer a
   short-lived / fine-grained token (or a GitHub Actions GITHUB_TOKEN) over a
   long-lived personal PAT, and delete throwaway tokens after use.

7. Commit `requirements.in` and `requirements.txt` together in ONE reviewed PR
   so the change is deliberate and auditable.

## Current verified baseline (update after each re-lock + push)
- Image digest: ghcr.io/bankingscience/bslagenticquantdevloop/sandbox@sha256:0e8d7b5e826ab4e9985c8b4a5ba281e0a359f2dc2c7b9b74889aef87b5cdc43d
- Base image: python:3.12-slim pinned by digest in the Dockerfile (both stages)
- Locked from working-set.txt captured from a known-good running container