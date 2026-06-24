#!/bin/bash
set -e

echo "=== Broadcast Messenger — Fly.io + Neon deploy ==="
echo ""
echo "Prerequisites:"
echo "  1. Neon database URL from https://neon.tech"
echo "  2. flyctl installed (brew install flyctl)"
echo "  3. fly auth login"
echo ""

if ! command -v fly &>/dev/null; then
  echo "Error: flyctl not found. Install: brew install flyctl"
  exit 1
fi

read -p "Neon DATABASE_URL: " DATABASE_URL
read -p "Facebook App ID: " FACEBOOK_APP_ID
read -p "Facebook App Secret: " FACEBOOK_APP_SECRET

if [ ! -f fly.toml ]; then
  echo "Running fly launch..."
  fly launch --no-deploy
fi

APP_NAME=$(grep '^app = ' fly.toml | cut -d'"' -f2)
APP_URL="https://${APP_NAME}.fly.dev"

echo ""
echo "Setting secrets for ${APP_URL}..."

fly secrets set \
  SECRET_KEY="$(openssl rand -hex 32)" \
  APP_URL="${APP_URL}" \
  DATABASE_URL="${DATABASE_URL}" \
  FACEBOOK_APP_ID="${FACEBOOK_APP_ID}" \
  FACEBOOK_APP_SECRET="${FACEBOOK_APP_SECRET}"

echo ""
echo "Deploying..."
fly deploy

echo ""
echo "Done! App URL: ${APP_URL}"
echo ""
echo "Next: add this to Facebook Login for Business → Valid OAuth Redirect URIs:"
echo "  ${APP_URL}/auth/facebook/callback"
echo ""
echo "And set App domains in Meta: ${APP_NAME}.fly.dev"
