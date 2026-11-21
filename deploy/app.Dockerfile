# DataPilot Streamlit app image (the analysis code itself runs in the separate datapilot-sandbox image).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 HOME=/tmp PIP_NO_CACHE_DIR=1
WORKDIR /opt/datapilot
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY *.py ./
COPY .streamlit .streamlit
COPY sample_data sample_data
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
