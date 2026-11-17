# Agent HQ egress proxy: the workbenches' only way out, with rules per workbench
# (see egress_proxy.py). Rules come from Valkey (AHQ_REDIS_URL), edited in the UI.
FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir "redis>=5.2"
COPY egress_proxy.py .
RUN useradd --system --no-create-home egress
USER egress
EXPOSE 8888
CMD ["python", "egress_proxy.py"]
