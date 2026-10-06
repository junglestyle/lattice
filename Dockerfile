# Lattice's image (Idea Machine's install/nas.sh builds it).
FROM python:3.13-slim
RUN pip install --no-cache-dir uv==0.12.2
# Install from a throwaway copy: the host clone's files may be root-only, which the runtime user can't read.
COPY pyproject.toml uv.lock README.md /src/
COPY src /src/src
RUN cd /src && UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev --no-editable && rm -rf /src /root/.cache
ENV PATH=/opt/venv/bin:$PATH
USER 568:568
EXPOSE 8790
# 0.0.0.0 inside the container only; compose publishes it on the host's loopback.
CMD ["lattice", "serve", "--host", "0.0.0.0", "--port", "8790"]
