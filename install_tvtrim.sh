#!/bin/bash
###
### install_tvtrim.sh - Install and configure the tvtrim pipeline
###
### Installs Comskip, ffmpeg, and sets up cron for automated
### commercial stripping of HDHomeRun OTA recordings.
###
### Usage: sudo ./install_comskip.sh
###

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMSKIP_USER="deck"
COMSKIP_HOME="/home/${COMSKIP_USER}/comskip"
COMSKIP_REPO="https://github.com/erikkaashoek/Comskip.git"
BUILD_DIR="/tmp/comskip_build"

###
### Colors for output
###
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

###
### Check prerequisites
###

if [ "$(id -u)" -ne 0 ]; then
    error "This script must be run as root (use sudo)."
    exit 1
fi

info "Starting tvtrim installation..."

###
### Handle SteamOS read-only filesystem
###

STEAMOS=false
if [ -f /etc/os-release ] && grep -q "SteamOS" /etc/os-release; then
    STEAMOS=true
    info "SteamOS detected. Disabling read-only filesystem..."
    steamos-readonly disable
    if [ $? -ne 0 ]; then
        error "Failed to disable read-only filesystem."
        exit 1
    fi
    info "Read-only filesystem disabled."
fi

###
### Initialize pacman keyring if needed
###

if ! pacman-key --list-keys &>/dev/null; then
    info "Initializing pacman keyring..."
    pacman-key --init
    pacman-key --populate archlinux
fi

###
### Install system dependencies
###

info "Installing system dependencies..."

# Core packages
# Use --overwrite to handle SteamOS file conflicts (e.g. git man pages)
pacman -S --needed --noconfirm --overwrite '*' \
    ffmpeg \
    base-devel \
    git \
    python \
    argtable \
    autoconf \
    automake \
    libtool \
    pkgconf

if [ $? -ne 0 ]; then
    error "Failed to install system packages."
    exit 1
fi

info "System dependencies installed."

###
### Build Comskip from source
###

if command -v comskip &>/dev/null; then
    CURRENT_VERSION=$(comskip --version 2>&1 | head -1 || echo "unknown")
    info "Comskip already installed: ${CURRENT_VERSION}"
    read -p "Reinstall/update Comskip? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        info "Skipping Comskip build."
        SKIP_BUILD=true
    fi
fi

if [ "${SKIP_BUILD}" != "true" ]; then
    info "Building Comskip from source..."

    # Clean up any previous build
    rm -rf "${BUILD_DIR}"
    mkdir -p "${BUILD_DIR}"

    cd "${BUILD_DIR}"
    git clone "${COMSKIP_REPO}" .

    if [ $? -ne 0 ]; then
        error "Failed to clone Comskip repository."
        exit 1
    fi

    # Build
    ./autogen.sh
    ./configure
    make -j$(nproc)

    if [ $? -ne 0 ]; then
        error "Failed to compile Comskip."
        exit 1
    fi

    # Install
    make install

    if [ $? -ne 0 ]; then
        error "Failed to install Comskip."
        exit 1
    fi

    info "Comskip built and installed successfully."

    # Clean up build directory
    rm -rf "${BUILD_DIR}"
fi

###
### Create directory structure
###

info "Setting up directory structure..."

mkdir -p "${COMSKIP_HOME}/logs"
chown -R "${COMSKIP_USER}:${COMSKIP_USER}" "${COMSKIP_HOME}/logs"

###
### Initialize database
###

info "Initializing database..."
sudo -u "${COMSKIP_USER}" python3 -c "
import sys
sys.path.insert(0, '${COMSKIP_HOME}')
import db
db.init_db('${COMSKIP_HOME}/tvtrim.db')
print('Database initialized successfully.')
"

###
### Set up cron job
###

info "Setting up cron job..."

cat <<EOF >/etc/cron.d/tvtrim
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

# Run tvtrim processing every hour at :00
00 * * * * ${COMSKIP_USER} /usr/bin/python3 ${COMSKIP_HOME}/tvtrim.py >> ${COMSKIP_HOME}/logs/cron.log 2>&1
EOF

chmod 644 /etc/cron.d/tvtrim
info "Cron job installed at /etc/cron.d/tvtrim"

###
### Re-enable SteamOS read-only filesystem (optional)
###

if [ "${STEAMOS}" = true ]; then
    warn "SteamOS read-only filesystem is currently DISABLED."
    warn "Installed packages may be lost on SteamOS updates."
    warn "To re-enable: sudo steamos-readonly enable"
    echo ""
    read -p "Re-enable read-only filesystem now? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        steamos-readonly enable
        info "Read-only filesystem re-enabled."
    else
        warn "Read-only filesystem left DISABLED."
    fi
fi

###
### Verify installation
###

info "Verifying installation..."

echo ""
echo "=== Installation Verification ==="
echo ""

# Check comskip
if command -v comskip &>/dev/null; then
    echo -e "  Comskip:  ${GREEN}OK${NC} ($(comskip --version 2>&1 | head -1 || echo 'installed'))"
else
    echo -e "  Comskip:  ${RED}NOT FOUND${NC}"
fi

# Check ffmpeg
if command -v ffmpeg &>/dev/null; then
    echo -e "  ffmpeg:   ${GREEN}OK${NC} ($(ffmpeg -version 2>&1 | head -1))"
else
    echo -e "  ffmpeg:   ${RED}NOT FOUND${NC}"
fi

# Check ffprobe
if command -v ffprobe &>/dev/null; then
    echo -e "  ffprobe:  ${GREEN}OK${NC}"
else
    echo -e "  ffprobe:  ${RED}NOT FOUND${NC}"
fi

# Check python
if command -v python3 &>/dev/null; then
    echo -e "  Python:   ${GREEN}OK${NC} ($(python3 --version))"
else
    echo -e "  Python:   ${RED}NOT FOUND${NC}"
fi

# Check database
if [ -f "${COMSKIP_HOME}/tvtrim.db" ]; then
    echo -e "  Database: ${GREEN}OK${NC} (${COMSKIP_HOME}/tvtrim.db)"
else
    echo -e "  Database: ${RED}NOT FOUND${NC}"
fi

# Check cron
if [ -f "/etc/cron.d/tvtrim" ]; then
    echo -e "  Cron:     ${GREEN}OK${NC} (/etc/cron.d/tvtrim)"
else
    echo -e "  Cron:     ${RED}NOT FOUND${NC}"
fi

# Check config files
if [ -f "${COMSKIP_HOME}/tvtrim.conf" ]; then
    echo -e "  Config:   ${GREEN}OK${NC} (${COMSKIP_HOME}/tvtrim.conf)"
else
    echo -e "  Config:   ${YELLOW}MISSING${NC} (expected: ${COMSKIP_HOME}/tvtrim.conf)"
fi

if [ -f "${COMSKIP_HOME}/comskip.ini" ]; then
    echo -e "  Comskip INI: ${GREEN}OK${NC} (${COMSKIP_HOME}/comskip.ini)"
else
    echo -e "  Comskip INI: ${YELLOW}MISSING${NC} (expected: ${COMSKIP_HOME}/comskip.ini)"
fi

echo ""
info "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Test with a single file:"
echo "     python3 ${COMSKIP_HOME}/tvtrim.py --dry-run"
echo "     python3 ${COMSKIP_HOME}/tvtrim.py --file \"/television/path/to/episode.mpg\""
echo ""
echo "  2. The cron job will automatically process recordings every hour."
echo ""
echo "  3. View logs:"
echo "     tail -f ${COMSKIP_HOME}/logs/tvtrim_\$(date +%Y%m%d).log"
echo ""
echo "  4. Check statistics:"
echo "     python3 ${COMSKIP_HOME}/tvtrim.py --stats"
echo ""
