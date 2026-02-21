# Multi-stage Dockerfile for Estimator King

# Stage 1: Base
FROM python:3.11-alpine AS base

WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy package code
COPY estimator_king/ estimator_king/

# Stage 2: Crawler
FROM base AS crawler

RUN pip install --no-cache-dir gunicorn

ENTRYPOINT ["python", "-m", "estimator_king.crawler"]

# Stage 3: Bot
FROM base AS bot

RUN pip install --no-cache-dir python-dotenv

ENTRYPOINT ["python", "-m", "estimator_king.bot"]
