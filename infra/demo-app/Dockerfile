FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY demo_app ./demo_app
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "demo_app.server"]
CMD ["order"]
