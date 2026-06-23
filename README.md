# CCTV Dashboard

This folder contains the static dashboard site for the CCTV detector project.

## Folder structure

cctv-dashboard/
  index.html              # landing page
  assets/                 # CSS, images, icons, and other static assets
  config/                 # Nginx config snippets or environment files
  scripts/                # helper scripts (optional)
  README.md               # setup and deployment notes

## Raspberry Pi 5 deployment notes

- Run the dashboard from the Raspberry Pi 5 that also hosts the detector service.
- Serve it through Nginx as a reverse proxy on port 8880.
- Keep the detector script and web dashboard in the same project tree for simple management.

**http://<RPI5-IP>:8881/**

<img width="751" height="548" alt="image" src="https://github.com/user-attachments/assets/2b4f7ac3-c39f-47a6-ad5a-7316ad27954d" />


**footage**
<img width="1109" height="647" alt="image" src="https://github.com/user-attachments/assets/57f1a755-6c0c-47bf-9113-a1dcdaaa8bbf" />

