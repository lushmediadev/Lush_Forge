# Lush Forge deployment and account-scoped LoRA

## Current runtime

- Public entry: https://ai.lushmedia.net through Caddy and Hub authentication.
- VPS runtime currently at /opt/lush-forge-hub/app; service lush-forge-hub.service binds 172.17.0.1:8036.
- Runtime data is outside source: /var/lib/lush-forge-hub and /etc/lush-forge-hub.env.
- Existing Python virtualenv is /opt/lush-forge-hub/app/.venv.
- Forge 1/2 are loopback-only with reverse ports 18386/18387; native history extension is installed on both.
- Repository https://github.com/lushmediadev/Lush_Forge was empty at task start. GitHub write access has since been granted to pearhoang.

## Active behavior

- Account root-level LoRAs are shared with every account assigned to that Forge.
- User uploads are private by account ID, use a collision-free Forge name, and are filtered from cards and generation requests for other accounts.
- The FLUX 8-step LoRA exists on both workers with the same SHA-256.

## Rollout plan

1. Push sanitized source to the GitHub repository.
2. Clone to /opt/lush-forge-hub/repo and update the systemd unit to run code from the Git checkout, retaining the current external environment, database, and venv.
3. Keep the existing app directory as rollback until public health and generation checks pass.
4. Deploy account-manifest, private upload, and generation-validation code to Hub and both worker extensions. Check queue empty before worker restarts.
5. Verify account A can see shared and own LoRAs, account B sees shared and only its own, and cross-account prompt tags are rejected.
