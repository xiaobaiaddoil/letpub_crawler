#!/bin/sh
set -eu

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_DIR"

git_tag() {
  git rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d%H%M%S
}

build_one() {
  target="$1"
  image="$2"

  docker build \
    --target "$target" \
    --build-arg "PYTHON_BASE_IMAGE=${PYTHON_BASE_IMAGE}" \
    --build-arg "PLAYWRIGHT_BASE_IMAGE=${PLAYWRIGHT_BASE_IMAGE}" \
    --build-arg "UV_VERSION=${UV_VERSION}" \
    --tag "$image" \
    "$PROJECT_DIR"

  printf 'built %s image: %s\n' "$target" "$image"

  if [ "${SAVE_TAR:-0}" = "1" ]; then
    mkdir -p "$PROJECT_DIR/dist"
    safe_name="$(printf '%s' "$image" | tr '/:' '__')"
    tar_file="$PROJECT_DIR/dist/${safe_name}.tar.gz"
    docker save "$image" | gzip -c > "$tar_file"
    sha256sum "$tar_file" > "${tar_file}.sha256"
    printf 'saved image: %s\n' "$tar_file"
  fi
}

IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-letpub-crawler}"
IMAGE_TAG="${IMAGE_TAG:-$(git_tag)}"
BUILD_TARGET="${BUILD_TARGET:-web}"
PYTHON_BASE_IMAGE="${PYTHON_BASE_IMAGE:-public.ecr.aws/docker/library/python:3.12-slim}"
PLAYWRIGHT_BASE_IMAGE="${PLAYWRIGHT_BASE_IMAGE:-mcr.microsoft.com/playwright/python:v1.57.0-noble}"
UV_VERSION="${UV_VERSION:-0.11.15}"

WEB_IMAGE="${LETPUB_WEB_IMAGE:-${IMAGE_REPOSITORY}:web-${IMAGE_TAG}}"
WORKER_IMAGE="${LETPUB_WORKER_IMAGE:-${IMAGE_REPOSITORY}:worker-${IMAGE_TAG}}"

export DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}"

case "$BUILD_TARGET" in
  web)
    build_one web "$WEB_IMAGE"
    ;;
  worker)
    build_one worker-slim "$WORKER_IMAGE"
    ;;
  worker-playwright)
    build_one worker "$WORKER_IMAGE"
    ;;
  all)
    build_one web "$WEB_IMAGE"
    build_one worker-slim "$WORKER_IMAGE"
    ;;
  *)
    echo "BUILD_TARGET must be web, worker, worker-playwright, or all" >&2
    exit 2
    ;;
esac
