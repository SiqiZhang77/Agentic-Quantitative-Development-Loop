# Experiment 2 model and runtime freeze checklist

The Jira command value `model: qwen3-coder` identifies a LiteLLM public alias,
not necessarily one immutable Qwen checkpoint. Before the formal C0/C1 runs,
record the mapping and execution limits below without copying any credential.

## Why this is part of the experiment rather than operations trivia

- A LiteLLM alias may be remapped or may load-balance across multiple
  deployments. If C0 and C1 reach different checkpoints, their difference is
  not attributable only to RAG.
- `temperature`, `top_p`, `max_tokens`, and seed handling affect output
  variability and the chance that a coding task finishes.
- High concurrency can cause queueing, HTTP 429, retries, or fallback routing.
  If one condition experiences more load, infrastructure is a confounder.
- C1 has a longer prompt. Equal token/cost limits are necessary for a fair
  comparison, while the extra tokens and cost remain an outcome to report.
- A fallback model can silently make a run incomparable unless the actual
  deployment used by each run is recorded.

This work does not block Jira extraction and corpus construction, but it must be
finished before the formal pilot. Do not fill it in retrospectively after
seeing C0/C1 results.

## Safe freeze record

Ask the LiteLLM operator to provide a redacted record with this shape:

The repository also contains a fillable
[`model_runtime_freeze.template.json`](model_runtime_freeze.template.json).

```json
{
  "captured_at_utc": "2026-08-10T00:00:00Z",
  "litellm_server_version": "REQUIRED",
  "server_image_tag_or_digest": "REQUIRED",
  "client_alias": "qwen3-coder",
  "deployments": [
    {
      "sanitized_model_id": "REQUIRED",
      "litellm_params_model": "REQUIRED",
      "provider_or_serving_backend": "REQUIRED",
      "checkpoint_or_deployment_revision": "REQUIRED_IF_AVAILABLE",
      "region": "REQUIRED_IF_ROUTING_DEPENDS_ON_IT",
      "rpm": null,
      "tpm": null,
      "weight": null
    }
  ],
  "routing": {
    "strategy": "REQUIRED",
    "retries": "REQUIRED",
    "fallbacks": "REQUIRED",
    "timeout_seconds": "REQUIRED",
    "cache_enabled": "REQUIRED"
  },
  "sampling_defaults": {
    "temperature": "REQUIRED_OR_EXPLICITLY_UNSET",
    "top_p": "REQUIRED_OR_EXPLICITLY_UNSET",
    "seed": "REQUIRED_OR_EXPLICITLY_UNSET",
    "max_tokens": "REQUIRED_OR_EXPLICITLY_UNSET",
    "drop_params": "REQUIRED"
  },
  "seed_support": {
    "status": "supported|rejected|silently_ignored|unknown",
    "evidence": "admin config, provider documentation, and/or controlled probe"
  },
  "experiment_virtual_key_or_team_limits": {
    "allowed_models": ["qwen3-coder"],
    "rpm": null,
    "tpm": null,
    "max_parallel_requests": null,
    "max_budget": null,
    "budget_reset_period": null
  },
  "sanitized_configuration_sha256": "REQUIRED"
}
```

`null` must mean “confirmed unset/unlimited”, not “we did not check”. Use the
string `unknown` when the operator cannot determine a value.

Never put API keys, the master key, database URLs/passwords, provider
credentials, UI passwords, Redis credentials, or raw environment-variable
values in this file.

## If you administer LiteLLM

### Admin UI

1. Open `<LiteLLM base URL>/ui` and authenticate using the normal admin method.
2. Open **Models + Endpoints → All Models**.
3. Search for `qwen3-coder`.
4. Record every matching row. Multiple rows mean the same alias can route to
   multiple deployments.
5. Open each Model ID and inspect Overview/Raw JSON. Record `model_name`, the
   non-secret `litellm_params.model`, provider/backend, model/deployment ID,
   RPM/TPM/weight, and non-secret sampling defaults.
6. Inspect router settings for strategy, retries, fallbacks, timeout, and cache.
7. Inspect the experiment virtual key/team for allowed models, RPM, TPM,
   parallel request limit, budget, and budget duration.
8. Export only a redacted record matching the schema above.

### File-based proxy

Find the config path from the LiteLLM service/container start command, usually a
`--config` argument. In the config and any included files, locate:

```yaml
model_list:
  - model_name: qwen3-coder
    litellm_params:
      model: provider/actual-model-or-deployment
```

Check all occurrences of `model_name: qwen3-coder`, not only the first one.
Also inspect model-specific `temperature`, `top_p`, `seed`, `max_tokens`,
`rpm`, `tpm`, and weight, plus `router_settings`, retries, fallbacks, caching,
and `drop_params`.

For Docker, the official pattern mounts a host config file into the container
and passes `--config /container/path`. Inspect only the command and mount paths;
do not paste a full `docker inspect` or expanded Compose configuration because
those outputs can contain credentials.

For Kubernetes, locate the Deployment argument/volume mount and the referenced
ConfigMap. Do not retrieve or share Kubernetes Secret values. For Helm, inspect
the release's non-secret `proxy_config.model_list` and check whether
`STORE_MODEL_IN_DB=True` is enabled.

### Database-managed models

When `STORE_MODEL_IN_DB=True`, models created through the Admin UI can coexist
with YAML models. The same alias can therefore have deployments from both
sources. The Admin UI is normally the safest complete view. An administrator
may also use the deployment's documented read-only `/model/info` endpoint and
provide a redacted response; do not give a student the LiteLLM master key.

## If you are not the administrator

Send the following request to the teammate or UCL operator responsible for the
LiteLLM service:

> For my MSc Experiment 2 reproducibility record, could you provide a redacted
> snapshot of the LiteLLM alias `qwen3-coder` at the experiment time? I need all
> underlying `litellm_params.model` deployments/model revisions, LiteLLM server
> version and image tag/digest, routing strategy/weights/retries/fallbacks,
> timeout and cache setting, default temperature/top_p/seed/max_tokens and
> `drop_params`, whether seed is supported/rejected/ignored, and the RPM, TPM,
> max-parallel-requests and budget/reset period for our experiment virtual
> key/team. Please include a UTC timestamp and a SHA-256 of the sanitized
> configuration. Please do **not** send any API key, master key, database
> password, provider credential, UI password, Redis secret, or raw config file.

If the operator cannot provide the mapping, record that the backend is unknown
and do a controlled probe before the pilot. A probe can establish whether a
parameter is accepted and whether repeated seeded calls appear stable, but it
cannot prove the exact checkpoint or guarantee full agent-loop determinism.

## Minimum experiment settings after the check

- Freeze one alias mapping/configuration for both conditions.
- Start with run concurrency `1`.
- Keep model, tools, iteration/turn/token/time budgets identical for C0 and C1.
- Randomize or balance which condition runs first within each paired task.
- If seed is supported, use the same predeclared seed schedule for each pair.
- If seed is unsupported or ignored, use repeated runs and report variability.
- Record the response model/deployment/request ID when the proxy exposes it.
- Treat an unexpected fallback or model/config drift as an invalid
  infrastructure run, not a C0/C1 success or failure.

## Official LiteLLM references

- [Proxy configuration and `model_list`](https://docs.litellm.ai/docs/proxy/configs)
- [Model management](https://docs.litellm.ai/docs/proxy/model_management)
- [Docker quick start](https://docs.litellm.ai/docs/proxy/docker_quick_start)
- [Production deployment](https://docs.litellm.ai/docs/proxy/deploy)
- [Virtual-key budgets and rate limits](https://docs.litellm.ai/docs/proxy/users)
