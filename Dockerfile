FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source and data
COPY h1b_matcher.py .
COPY app.py .
COPY ["Employer Information.csv", "."]

EXPOSE 5000

CMD ["python", "-X", "utf8", "app.py"]
