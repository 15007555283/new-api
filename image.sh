#!/bin/bash

MODE=$1
ACTION=$2
COMMIT_VERSION=$3

MODE=$(echo $MODE | tr '[:lower:]' '[:upper:]')
if [ -z "$MODE" ]; then
    MODE="PRO"
fi

IMAGE_VERSION_FILE="IMAGE_VERSION"_$MODE
CURRENT_VERSION="1.0.0"
VERSION_NUM=100

if [ -f "$IMAGE_VERSION_FILE" ]; then
    CURRENT_VERSION=$(cat $IMAGE_VERSION_FILE)
fi

MAJOR=$(echo $CURRENT_VERSION | cut -d. -f1)
MINOR=$(echo $CURRENT_VERSION | cut -d. -f2)
PATCH=$(echo $CURRENT_VERSION | cut -d. -f3)

if [ "$PATCH" -ge $VERSION_NUM ]; then
    PATCH=1
    MINOR=$((MINOR + 1))
else
    PATCH=$((PATCH + 1))
fi
if [ "$MINOR" -gt $VERSION_NUM ]; then
    MINOR=1
    MAJOR=$((MAJOR + 1))
fi

NEW_VERSION="$MAJOR.$MINOR.$PATCH"

if [ "$ACTION" = "--commit" ]; then
    # 写入调用方指定的版本号（由 makefile 在构建成功后传入）
    TARGET=${COMMIT_VERSION:-$NEW_VERSION}
    echo $TARGET > $IMAGE_VERSION_FILE
elif [ "$ACTION" = "--read" ]; then
    # 只输出下一个版本号，不写文件
    echo $NEW_VERSION
else
    # 默认行为：计算并立即写入（兼容旧调用）
    echo $NEW_VERSION > $IMAGE_VERSION_FILE
    echo $NEW_VERSION
fi
