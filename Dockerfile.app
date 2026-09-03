FROM ubuntu:22.04

ARG VERSION=1.0.0
ARG NEW_API_OFFICIAL_TAG=v1.0.0-rc.30
ARG MODE=dev
ARG PROJECT_NAME=new-api
ARG PACKAGE_NAME=${PROJECT_NAME}_${MODE}.${VERSION}
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG APT_FALLBACK_MIRROR=mirrors.ustc.edu.cn

ENV PROJECT_NAME=${PROJECT_NAME}
ENV NEW_API_OFFICIAL_TAG=${NEW_API_OFFICIAL_TAG}
ENV VERSION=${NEW_API_OFFICIAL_TAG}

RUN set -eux; \
 sed -i "s@http://archive.ubuntu.com/ubuntu/@http://${APT_MIRROR}/ubuntu/@g" /etc/apt/sources.list; \
 sed -i "s@http://security.ubuntu.com/ubuntu/@http://${APT_MIRROR}/ubuntu/@g" /etc/apt/sources.list; \
 apt-get -o Acquire::Retries=5 update || ( \
   sed -i "s@${APT_MIRROR}@${APT_FALLBACK_MIRROR}@g" /etc/apt/sources.list && \
   apt-get -o Acquire::Retries=5 update \
 ); \
 apt-get install -y --no-install-recommends --fix-missing \
    ca-certificates \
    vim \
    net-tools \
    wget \
   curl \
   tzdata; \
 update-ca-certificates; \
 rm -rf /var/lib/apt/lists/*

ENV WORK_DIR=/data
RUN mkdir -p ${WORK_DIR}

WORKDIR ${WORK_DIR}

COPY ./dist/${PROJECT_NAME}-${MODE}.${VERSION} ${WORK_DIR}/${PROJECT_NAME}-${MODE}.${VERSION}
RUN ln -s ${WORK_DIR}/${PROJECT_NAME}-${MODE}.${VERSION} ${WORK_DIR}/${PROJECT_NAME}

RUN chmod +x ${WORK_DIR}/${PROJECT_NAME}

# 复制配置文件
COPY .env.${MODE} ${WORK_DIR}/.env

EXPOSE 3000
ENTRYPOINT ["sh", "-c", "exec ./$PROJECT_NAME"]
