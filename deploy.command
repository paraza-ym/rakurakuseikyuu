#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE="https://github.com/paraza-ym/rakurakuseikyuu.git"

cd "$REPO_DIR"

if [ ! -d ".git" ]; then
  git init
  git config user.name "paraza-ym"
  git config user.email "paraza.ymori@gmail.com"
  git remote add origin "$REMOTE"
  git config credential.helper osxkeychain
fi

git add .

if git diff --cached --quiet; then
  echo "変更なし。プッシュ不要です。"
  read -p "Enterキーで閉じる"
  exit 0
fi

TIMESTAMP=$(date "+%Y-%m-%d %H:%M")
git commit -m "deploy: $TIMESTAMP"

REMOTE_BRANCH=$(git ls-remote --symref "$REMOTE" HEAD 2>/dev/null | grep "ref:" | sed 's/ref: refs\/heads\///' | awk '{print $1}')
BRANCH=${REMOTE_BRANCH:-main}

git push origin "HEAD:$BRANCH" --force

echo ""
echo "✅ プッシュ完了 → https://github.com/paraza-ym/rakurakuseikyuu"
read -p "Enterキーで閉じる"
