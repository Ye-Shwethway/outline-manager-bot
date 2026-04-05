# Deployment Guide (Ubuntu VPS)

## 1. Security Checklist Before First Push

1. Rotate Telegram bot token immediately if it was ever committed/shared.
2. Keep secrets only in `.env` on your machine/VPS.
3. Commit `.env.example` instead of `.env`.
4. Ensure `.gitignore` and `.dockerignore` are present before `git add`.
5. Never commit `data/*.db` from local runs.

## 2. Files Added for Safe Deployment

- `.gitignore`
- `.dockerignore`
- `.env.example`
- `Dockerfile`
- `docker-compose.yml`

## 3. One-shot Ubuntu VPS Setup

Run this as a single block on the VPS (Ubuntu 22.04+):

```bash
set -e
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
mkdir -p ~/apps
cd ~/apps
# Replace with your repo URL
# git clone https://github.com/<org-or-user>/<repo>.git outline-bot
# cd outline-bot
```

## 4. App Environment on VPS

Create a production env file in project root:

```bash
cat > .env << 'EOF'
BOT_TOKEN=replace_with_new_token
OWNER_ID=replace_with_numeric_owner_id
EOF
chmod 600 .env
mkdir -p data
```

## 5. Start Service

```bash
docker compose up -d --build
```

## 6. Operations

```bash
# logs
docker compose logs -f --tail=200 outline-bot

# restart
docker compose restart outline-bot

# stop
docker compose down

# update to latest code
git pull
docker compose up -d --build
```
