# C2C Agent Bridge — hosts both agents (Campaign Identification + Campaign-in-a-Box)
# behind one FastAPI service. Test deployments: Render (this image). Production
# moves to Azure per the deployment plan; secrets always come from the host's
# environment settings, never from this image or the repo.

FROM python:3.12-slim

WORKDIR /app

# Editable installs keep the source tree layout, which the agents rely on for
# their versioned prompt files and Business Capability configs (loaded relative
# to the package source, not site-packages).
COPY shared/ shared/
COPY campaign-identification/ campaign-identification/
COPY campaign-in-a-box/ campaign-in-a-box/
COPY bridge/ bridge/
COPY levelshift-agent-starter-kit/schemas/ levelshift-agent-starter-kit/schemas/

RUN pip install --no-cache-dir -e ./shared -e ./campaign-identification \
    -e ./campaign-in-a-box -e ./bridge

ENV PYTHONUNBUFFERED=1 \
    STS_SCHEMA_PATH=/app/levelshift-agent-starter-kit/schemas/sts-core.schema.v2.0.0.json \
    BRIDGE_WORKDIR=/tmp/bridge-run

EXPOSE 8787

# Render injects PORT; default keeps local `docker run` working.
CMD ["sh", "-c", "uvicorn c2c_bridge.app:app --host 0.0.0.0 --port ${PORT:-8787}"]
