# app-deepvariant/Dockerfile

# Use the GPU-specific tag as required by the official documentation.
FROM google/deepvariant:1.9.0-gpu

# Switch to the root user to install packages
USER root

# CORRECTED: Install the self-contained AWS CLI v2 to avoid Python dependency conflicts.
# Also install samtools and clean up.
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    samtools \
    unzip \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" \
    && unzip awscliv2.zip \
    && ./aws/install \
    && rm -rf awscliv2.zip ./aws

# Create a working directory for all operations
WORKDIR /work

# Copy our entrypoint script into the image's PATH and make it executable
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Set the entrypoint to our script
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
