# Deploy CraftPOV

One Vercel project hosts the React workbench, the FastAPI web API, and a rendering daemon. Vite builds the workbench into `frontend/dist`. Vercel promotes those files, including the example film, to its CDN. The Python function handles intake, project reads, and progress streams. The daemon calls the generation providers and renders media. [FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi) · [Vercel Daemons](https://vercel.com/docs/daemons)

## Production setup

1. Import the repository into Vercel. Set the root directory to the repository root, the framework preset to **FastAPI**, and the Node.js version to **24.x**. Keep the package manifests, lockfiles, and bundled example media in the deployed Git revision. The checked-in build command installs the frontend dependencies and runs Vite. The Python package selects Python 3.14. [Vercel Python runtime](https://vercel.com/docs/functions/runtimes/python)
2. Create a Turso **libSQL** database and obtain its database URL and a read/write database token. Add the values as `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in the Vercel project's **Production** environment. The worker and API create the SQLite tables on first use. [Turso SQL over HTTP](https://docs.turso.tech/sdk/http/quickstart)
3. In Vercel **Storage**, create a **public Blob** store for this environment. Connect it to the project and select **Add a read-write token env var to this connection**. This supplies `BLOB_READ_WRITE_TOKEN` to the API and daemon. People with the media URL can access the corresponding file, including a supplied desk photo. Database content stays in Turso. [Vercel Blob Python SDK](https://vercel.com/docs/vercel-blob/python-sdk-reference)
4. Set `GEMINI_API_KEY`, `YOUTUBE_API_KEY`, and the model IDs available to that Gemini project. See [.env.example](.env.example) for the model settings. Store credentials in Vercel's server environment. The built frontend uses same-origin URLs and requires no API key or API base override.
5. Deploy to **Production**. Vercel builds the worker container from `src/` and starts one renderer in `iad1`. Each accepted submission can invoke generation services under the configured keys. Use the project's deployment access settings for your intended audience. [Deployment Protection](https://vercel.com/docs/deployment-protection)
6. Open `/api/health` and check `generation_ready`. Its message identifies missing configuration or an offline worker. Open the example in the workbench to verify video, captions and chapter seeking. Submit a short project to exercise your live provider credentials. Refresh the page during rendering to check that its saved progress loads.

Daemons currently run in production only and use provisioned memory while idle. The daemon entry in [vercel.json](vercel.json) controls instance count and shutdown grace time. Each instance can research and plan the next guide while it renders the current one. A higher instance count can render additional guides concurrently. [Daemon availability and quotas](https://vercel.com/docs/daemons)

## SQLite state and media

The local server creates `data/craftpov.sqlite3` and keeps generated files under `outputs/`. Override these paths with `SQLITE_PATH` and `MEDIA_OUTPUT_DIR`. The hosted API and daemon use the same SQLite tables and transactions through Turso's HTTP API. Both read the same project snapshot and event sequence across restarts and server instances.

The worker records a claim before it starts generating, renews it while rendering, and publishes a step once its files are available. If a worker disappears, the next claim checks expired leases, preserves published material, and marks the interrupted project partial or failed. Submitting a fresh project retries generation. The hosted event stream reconnects periodically, starts with a fresh snapshot, and continues from its stored revision.

Hosted MP4, WAV, image and caption requests redirect to Blob's CDN, which serves media and byte ranges to the player. Worker scratch files last for a single job. The hosted intake endpoint accepts JPEG, PNG or WebP desk photos up to 4 MiB, leaving room under Vercel's default function request limit. The local limit is 8 MiB. [Vercel Function limits](https://vercel.com/docs/functions/limitations)

## Local development and previews

From the repository root, run the local version with SQLite and an embedded worker.

```sh
uv sync --locked
npm --prefix frontend ci
npm --prefix frontend run build
uv run uvicorn nyc_gemini4_hack.api:app --host 127.0.0.1 --port 8000
```

Preview deployments serve the workbench and bundled example without production credentials. To generate custom guides on a preview, configure a separate Turso database, Blob store, and provider credentials for that environment. Start a worker on an existing host with Python 3.14 and those same environment variables. The worker reads its cloud environment at process startup.

```sh
uv sync --locked
uv run --env-file .env.preview.local python -m nyc_gemini4_hack.cloud_worker
```

To exercise the hosted FastAPI entrypoint locally against that preview configuration, build the frontend, then run `uv run --env-file .env.preview.local uvicorn app:app` from the repository root.

After changing Python dependencies, refresh the worker's pinned package list from the same lockfile. Vercel limits its container build context to the worker directory. [Daemon build context](https://vercel.com/docs/daemons/config-reference#root)

```sh
uv export --locked --no-dev --no-emit-project --no-hashes --no-annotate -o src/requirements.worker.txt
```
