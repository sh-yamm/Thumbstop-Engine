# Thumbstop Engine on a Hugging Face Space (Docker SDK, Streamlit on port 8501).
# deploy/push_space.py copies this to the Space's root as ./Dockerfile.
FROM python:3.12-slim

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    THUMBSTOP_DATA_DIR=/home/user/app/data \
    THUMBSTOP_RUNS_DIR=/home/user/app/runs
WORKDIR /home/user/app

COPY --chown=user requirements.txt ./requirements.txt
COPY --chown=user app/requirements.txt ./app/requirements.txt
RUN pip install --no-cache-dir --user -r app/requirements.txt

COPY --chown=user . .
EXPOSE 8501

# The keep-awake process (visits the Space's page every 15 minutes) runs next to Streamlit from boot.
CMD ["sh", "-c", "python -m app.keepalive & exec streamlit run app/streamlit_app.py --server.port=8501 --server.address=0.0.0.0 --server.headless=true"]
