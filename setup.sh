#!/bin/bash
set -e

PWD=$(pwd)

###############################################################################
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
	echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}
warn() {
	echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1"
}
error() {
	echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1"
	exit 1
}

###############################################################################
set_conf_line_to_file() {
	local file="$1"
	local id="$2"
	shift 2
	local line="$*"

	# Check if a line containing the id exists
	if grep -q "$id" "$file" 2>/dev/null; then
		# Replace the line containing the id
		sed -i "/$id/c $line" "$file"
	else
		# Add the line to the end of file
		echo "$line" >> "$file"
	fi
}

set_conf_file() {
	local target_file="$1"
	local source_file="$2"

	# Extract basename of source file for backup naming
	local backup_name="$(basename "$source_file").backup"

	# Check if backup already exists
	if [ -f "$backup_name" ]; then
		log "Backup $backup_name already exists, skipping backup creation..."
	else
		# Create backup of target file
		if [ -f "$target_file" ]; then
			sudo cp "$target_file" "$backup_name"
			log "Created backup: $backup_name"
		else
			log "Warning: Target file $target_file does not exist"
			return 1
		fi
	fi

	sudo cp "$source_file" "$target_file"
	log "Copied $source_file to $target_file"
}

jq_inplace() {
	local filter="$1"
	local target_file="$2"
	local tmp_file=$(basename "$2").tmp
	sudo jq "$filter" $target_file > $tmp_file && sudo mv $tmp_file $target_file
}

###############################################################################
load_configuration() {
	SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
	CONFIG_FILE="$SCRIPT_DIR/config.conf"

	if [ -f "$CONFIG_FILE" ]; then
		log "Loading configuration from $CONFIG_FILE"
		source "$CONFIG_FILE"
	else
		warn "Configuration file not found at $CONFIG_FILE"
		warn "Using default values or prompting for input"
	fi
}

###############################################################################
setup_essential_packages() {
	log "Updating system packages..."
	sudo apt-get update && sudo apt-get upgrade -y

	log "Installing essential packages..."
	sudo apt-get install -y neovim curl wget rsync jq $INSTALL_PACKAGES
}

###############################################################################
setup_external_drive() {
	if [ -z "$EXTERNAL_PARTITION_LABEL" ]; then
		return 0
	fi

	log "Looking for external partition with LABEL=$EXTERNAL_PARTITION_LABEL"
	local ext_part_name=$(lsblk -rno NAME,LABEL | grep $EXTERNAL_PARTITION_LABEL | cut -d' ' -f1)
	local ext_part_type=$(lsblk -rno FSTYPE,LABEL | grep $EXTERNAL_PARTITION_LABEL | cut -d' ' -f1)

	if [ -n "$ext_part_name" ]; then
		warn "Found external partition '$ext_part_name'"
		STORAGE_ROOT=/media/data
		set_conf_line_to_file /etc/fstab /media/data \
			LABEL=$EXTERNAL_PARTITION_LABEL /media/data $ext_part_type defaults,noatime 0 1
	else
		STORAGE_ROOT=/home/pi
		warn "No external partition found. Skipping drive setup."
		return 0
	fi

	# Mount the device
	if ! mountpoint -q /media/data; then
		log "Mounting /media/data..."
		sudo mount /media/data
	fi

	sudo mkdir $STORAGE_ROOT/cartoons
	sudo mkdir $STORAGE_ROOT/documents
	sudo mkdir $STORAGE_ROOT/downloads
	sudo mkdir $STORAGE_ROOT/icons
	sudo mkdir $STORAGE_ROOT/movies
	sudo mkdir $STORAGE_ROOT/music
	sudo mkdir $STORAGE_ROOT/pictures
	sudo mkdir $STORAGE_ROOT/retropie
	sudo mkdir $STORAGE_ROOT/rtorrent
	sudo mkdir $STORAGE_ROOT/tvshows
	sudo mkdir $STORAGE_ROOT/videos

	sudo chown pi:pi $STORAGE_ROOT/cartoons
	sudo chown pi:pi $STORAGE_ROOT/documents
	sudo chown pi:pi $STORAGE_ROOT/downloads
	sudo chown pi:pi $STORAGE_ROOT/icons
	sudo chown pi:pi $STORAGE_ROOT/movies
	sudo chown pi:pi $STORAGE_ROOT/music
	sudo chown pi:pi $STORAGE_ROOT/pictures
	sudo chown pi:pi $STORAGE_ROOT/retropie
	sudo chown pi:pi $STORAGE_ROOT/rtorrent
	sudo chown pi:pi $STORAGE_ROOT/tvshows
	sudo chown pi:pi $STORAGE_ROOT/videos

	cd $HOME
	ln -s $STORAGE_ROOT/cartoons 
	ln -s $STORAGE_ROOT/documents
	ln -s $STORAGE_ROOT/downloads
	ln -s $STORAGE_ROOT/icons
	ln -s $STORAGE_ROOT/movies
	ln -s $STORAGE_ROOT/music
	ln -s $STORAGE_ROOT/pictures
	ln -s $STORAGE_ROOT/retropie
	ln -s $STORAGE_ROOT/rtorrent
	ln -s $STORAGE_ROOT/tvshows
	ln -s $STORAGE_ROOT/videos
	cd -
}

###############################################################################
configure_locales() {
	log "Configuring locales..."
	sudo apt-get install -y locales-all

	sudo update-locale \
		LANG="$LOCALE_PRIMARY" \
		LANGUAGE="" \
		LC_NUMERIC="$LOCALE_SECONDARY" \
		LC_TIME="$LOCALE_SECONDARY" \
		LC_MONETARY="$LOCALE_SECONDARY" \
		LC_PAPER="$LOCALE_SECONDARY" \
		LC_NAME="$LOCALE_SECONDARY" \
		LC_ADDRESS="$LOCALE_SECONDARY" \
		LC_TELEPHONE="$LOCALE_SECONDARY" \
		LC_MEASUREMENT="$LOCALE_SECONDARY" \
		LC_IDENTIFICATION="$LOCALE_SECONDARY" \
		LC_ALL=""

	sudo locale-gen

	log "Locales configured successfully!"
}

###############################################################################
add_user_to_group() {
	local user="$1"
	local group="$2"

	if ! groups "$user" | grep -q "\b$group\b"; then
		log "Adding user '$user' to '$group' group..."
		sudo usermod -a -G "$group" "$user"
		return 1  # Group was added
	else
		log "User '$user' already in '$group' group"
		return 0  # User already in group
	fi
}

setup_groups() {
	local needs_reboot=false

	# 'lp' group enables user to use and modify printers
	add_user_to_group pi lp || needs_reboot=true

	add_user_to_group pi docker || needs_reboot=true

	# Exit if reboot needed
	if [ "$needs_reboot" = true ]; then
		warn "User groups have been modified. Please reboot the system for changes to take effect."
		log "After reboot, run this script again to continue the setup."
		exit 0
	fi

	log "User groups are properly configured, continuing..."
}

###############################################################################
install_cups() {
	log "Installing CUPS printing system..."
	sudo apt-get install -y cups printer-driver-splix

	cupsctl --remote-admin --remote-any --share-printers

	sudo systemctl restart cups.service
	log "CUPS installation and configuration completed!"
}

###############################################################################
install_samba() {
	log "Installing Samba file sharing..."
	sudo apt-get install -y samba samba-common-bin

	set_conf_file /etc/samba/smb.conf samba/smb.conf

	if sudo pdbedit -L | grep -q "pi:"; then
		log "Samba user 'pi' already exists"
	else
		sudo smbpasswd -a pi
	fi

	sudo systemctl restart samba
}

###############################################################################
install_transmission() {
	log "Installing Transmission BitTorrent daemon..."
	sudo apt-get install -y transmission-daemon

	# Stop service to modify configuration
	sudo systemctl stop transmission-daemon

	local conf_file=/etc/transmission-daemon/settings.json

	jq_inplace '."rpc-whitelist-enabled" = false' $conf_file
	jq_inplace '."rpc-host-whitelist-enabled" = false' $conf_file
	jq_inplace '."rpc-authentication-required" = true' $conf_file
	jq_inplace '."ratio-limit" = 2' $conf_file
	jq_inplace '."ratio-limit-enabled" = true' $conf_file
	jq_inplace ".\"download-dir\" = \"$STORAGE_ROOT/downloads\"" $conf_file

	sudo sed -i 's/debian-transmission/pi/' /lib/systemd/system/transmission-daemon.service
	sudo systemctl daemon-reload

	sudo systemctl start transmission-daemon
	log "Transmission installation completed!"
}

###############################################################################
install_docker() {
	log "Installing Docker..."

	# Check if Docker is already installed
	if command -v docker > /dev/null; then
		log "Docker is already installed"
	else
		curl -sSL https://get.docker.com | sh
	fi

	log "Docker installation completed!"
}

###############################################################################
install_immich() {
	log "Installing Immich photo management server..."

	# Create apps directory for Immich
	mkdir -p /home/pi/apps
	cd /home/pi/apps

	curl -o- https://raw.githubusercontent.com/immich-app/immich/main/install.sh | bash

	sudo mkdir -p $STORAGE_ROOT/immich
	sudo chown pi:pi $STORAGE_ROOT/immich

	# Update Immich .env file to use external storage
	if [ -f "/home/pi/apps/immich-app/.env" ]; then
		log "Configuring Immich to use external storage..."
		sed -i "s|UPLOAD_LOCATION=.*|UPLOAD_LOCATION=$STORAGE_ROOT/immich|" /home/pi/apps/immich-app/.env
	fi

	cd /home/pi
	log "Immich installation completed!"
}

###############################################################################
install_nginx() {
	log "Installing Nginx..."

	sudo apt-get install -y nginx

	sudo rsync -avr "$SCRIPT_DIR/nginx/www" /var/
	set_conf_file /etc/nginx/sites-available/default nginx/default
	sudo nginx -t && sudo systemctl restart nginx

	log "Nginx installed!"
}
###############################################################################
install_nginxproxymanager() {
	log "Installing Nginxproxymanager..."

	cd $PWD/nginxproxymanager
	docker compose up -d

	log "Nginxproxymanager installed!"
}

###############################################################################
# Check if running as root
if [[ $EUID -eq 0 ]]; then
   error "This script should not be run as root. Please run as pi user."
fi

log "Starting Raspberry Pi Server Setup..."

#load_configuration
#setup_essential_packages
#configure_locales
#setup_external_drive
#setup_groups
#install_cups
#install_samba
#install_transmission
#install_docker
#install_immich
#install_nginx
install_nginxproxymanager

log "All done"
