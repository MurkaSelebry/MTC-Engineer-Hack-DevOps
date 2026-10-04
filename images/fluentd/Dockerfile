# Immutable linux/amd64 manifest for official Ruby 3.4.11 slim-bookworm.
ARG RUBY_IMAGE=docker.io/library/ruby:3.4.11-slim-bookworm@sha256:bcb412ff5c2bdace9af8171c410745e227aea813fa951caab539b2f18623d239
FROM ${RUBY_IMAGE} AS build
# Snapshot fixes compiler/header dependency versions as well as the base image.
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf '%s\n' 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20260928T000000Z bookworm main' > /etc/apt/sources.list && \
    apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 update && apt-get install -y --no-install-recommends build-essential libssl-dev libyaml-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /fluentd
COPY Gemfile Gemfile.lock ./
ENV BUNDLE_PATH=/opt/bundle BUNDLE_FROZEN=true BUNDLE_FORCE_RUBY_PLATFORM=true
RUN gem install bundler -v 2.6.8 --no-document && \
    bundle _2.6.8_ install --jobs 4 && \
    bundle _2.6.8_ clean --force && \
    rm -rf /opt/bundle/ruby/3.4.0/cache

FROM ${RUBY_IMAGE}
WORKDIR /fluentd
ENV BUNDLE_PATH=/opt/bundle BUNDLE_FROZEN=true BUNDLE_FORCE_RUBY_PLATFORM=true
COPY --from=build /usr/local/bundle /usr/local/bundle
COPY --from=build /opt/bundle /opt/bundle
COPY Gemfile Gemfile.lock ./
RUN mkdir -p /fluentd/etc /var/lib/fluentd /tmp && \
    bundle _2.6.8_ exec fluentd --version
ENTRYPOINT ["bundle", "_2.6.8_", "exec", "fluentd"]
CMD ["--no-supervisor", "-c", "/fluentd/etc/fluent.conf"]
