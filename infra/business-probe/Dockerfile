FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY backend/app/business_checks ./backend/app/business_checks
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "backend.app.business_checks.server"]
