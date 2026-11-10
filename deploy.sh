#!/bin/sh

set -e

docker buildx build -t gabay/docat --platform linux/amd64,linux/arm64 --push .
