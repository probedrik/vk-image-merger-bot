FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY vk_bot.py .
COPY yadisk_service.py .
COPY your_image_script.py .

# Create directories for downloads and cache
RUN mkdir -p downloads

# Note: .env should be provided at runtime via --env-file or -e flags
# Do NOT copy .env into the image!

CMD ["python", "vk_bot.py"]
