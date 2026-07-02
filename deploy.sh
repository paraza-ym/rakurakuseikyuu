#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE="https://github.com/paraza-ym/rakurakuseikyuu.git"

cd "$REPO_DIR"

# git未初期化なら初期化
if [ ! -d ".git" ]; then
  git init
  git config user.name "paraza-ym"
  git config user.email "paraza.ymori@gmail.com"
  git remote add origin "$REMOTE"
  # macOSキーチェーンで認証情報を保存
  git config credential.helper osxkeychain
fi

git add .

# 変更がなければ終了
if git diff --cached --quiet; then
  echo "変更なし。プッシュ不要です。"
  exit 0
fi

TIMESTAMP=$(date "+%Y-%m-%d %H:%M")
git commit -m "deploy: $TIMESTAMP"

# リモートのブランチ名を確認して合わせる
REMOTE_BRANCH=$(git ls-remote --symref "$REMOTE" HEAD 2>/dev/null | grep "ref:" | sed 's/ref: refs\/heads\///' | awk '{print $1}')
BRANCH=${REMOTE_BRANCH:-main}

git push origin "HEAD:$BRANCH" --force

echo ""
echo "✅ プッシュ完了 → https://github.com/paraza-ym/rakurakuseikyuu"
