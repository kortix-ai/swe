sudo apt update
sudo apt upgrade -y
sudo apt-get install -y python3
sudo apt-get install -y python3-pip
sudo apt-get install -y python-is-python3
sudo apt-get install -y python3-venv

sudo apt install -y apt-transport-https ca-certificates curl software-properties-common
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io
sudo systemctl enable docker

python -m venv .venv
source .venv/bin/activate
echo "source $(pwd)/.venv/bin/activate" >> ~/.bashrc
source ~/.bashrc


docker --version
python --version