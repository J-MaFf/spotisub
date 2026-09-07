FROM python:3.10-slim-bookworm

# Bumped from python:3.10-slim-bullseye (2026-09-07): Debian 11 is EOL and
# deb.debian.org has pruned packages this build pinned to (apt-get update
# succeeded but install 404s on libaom0/libglx-mesa0/libc-dev-bin/
# linux-libc-dev). Repointing at archive.debian.org isn't a clean fix
# either -- that mirror never carried a bullseye-security archive at all
# (only pre-2014 releases have one there), and dropping -security produces
# real dependency conflicts (libstdc++-10-dev needs a newer libc6-dev than
# the archived non-security repo has). Bookworm is current stable with a
# live, unpruned mirror, which sidesteps the whole class of problem.

# Install dependencies and gosu
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        gcc \
        g++ \
        ffmpeg \
        curl 
        
RUN curl -LO https://github.com/tianon/gosu/releases/latest/download/gosu-$(dpkg --print-architecture | awk -F- '{ print $NF }') \
        && chmod 0755 gosu-$(dpkg --print-architecture | awk -F- '{ print $NF }') \
        && mv gosu-$(dpkg --print-architecture | awk -F- '{ print $NF }') /usr/local/bin/gosu

RUN rm -rf /var/lib/apt/lists/*

RUN useradd -ms /bin/bash user

USER user
ENV HOME=/home/user
ENV PATH="$HOME/.local/bin:$PATH"

WORKDIR $HOME/spotisub

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt
COPY main.py config.py config_gunicorn.py init.py entrypoint.sh first_run.sh ./
COPY spotisub spotisub/

USER root
RUN chmod +x entrypoint.sh && \
    chmod +x first_run.sh && \
    chown -R user:user .

# CMD runs as root initially but switches to the user inside entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
