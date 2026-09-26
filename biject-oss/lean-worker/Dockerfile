FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    curl git build-essential python3 \
    && rm -rf /var/lib/apt/lists/*

# Install elan; the toolchain itself comes from the policy project's lean-toolchain file.
RUN curl https://elan.lean-lang.org/elan-init.sh -sSf | sh -s -- -y --default-toolchain none
ENV PATH="/root/.elan/bin:$PATH"

# The Proofread Lean policies (proofread/policies/lean, passed as the `policies` build context) are
# compiled into the image. Any local .lake is dropped: it may hold another platform's build.
WORKDIR /app
COPY --from=policies . ./PolicyEnv/
RUN cd PolicyEnv && rm -rf .lake && lean --version && lake build Proofread

COPY worker.py .

ENV POLICY_ENV_DIR=/app/PolicyEnv \
    LEAN_IMPORTS=Proofread \
    LEAN_BUILD_ON_START=0
EXPOSE 9000
CMD ["python3", "worker.py"]
