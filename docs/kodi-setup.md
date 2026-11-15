# Connect Kodi to Home Media Catalog

You can import an existing Kodi video library directly into Home Media Catalog on iPhone/iPad. The Home Media Catalog Docker server and MariaDB are **not required** for a normal Kodi JSON-RPC import. You can also configure the same Kodi connection in the optional web/server application.

## 1. Enable Kodi's built-in web server

On the device running Kodi:

1. Open **Settings** (gear icon) → **Services** → **Control**.
2. Enable **Allow remote control via HTTP**.
3. Note the HTTP port (typically **8080**; use the value shown in Kodi).
4. Keep **Require authentication** enabled and set a username and a strong password.
5. For live remote-control events and WebSocket features, enable **Allow remote control from applications on other systems** in the Application control section. Kodi normally uses TCP **9090** for JSON-RPC WebSocket connections. Only enable this on a trusted local network.
6. Optionally enable Zeroconf under **Services → General → Announce services to other systems**, if available on your Kodi platform, to make discovery easier.

Find the device's local IP address in Kodi under **Settings → System information → Network** (or in your router).

## 2. Test the connection

Connect your iPhone/iPad to the same network as Kodi. In Safari, enter the Kodi address, for example:

`http://192.168.1.50:8080/`

Replace the example IP address and port with your own. Enter the Kodi username and password when prompted. Kodi's web interface should open. If not, verify the IP address, port, network and firewall before configuring Home Media Catalog.

## 3. Add Kodi in Home Media Catalog

### On iPhone/iPad

1. Open **Settings → Sources and synchronization → Add a source**.
2. Select **Kodi** and give the source a name.
3. In the initial Kodi JSON-RPC instance section, discover Kodi on the local network or enter its host/IP address, HTTP port, username and password. Use the WebSocket port configured for your setup (normally 9090). Enable Kodi HTTPS only if your Kodi endpoint actually supports HTTPS.
4. Tap **Test Kodi JSON-RPC** and then **Save**.
5. In the source list, start the Kodi import/synchronization if it has not already started. Return to the Catalog tab to browse the imported titles.

Allow the iOS Local Network permission if prompted. To enrich missing artwork and metadata, configure your own TMDB API key or read-access token in the application's TMDB settings. It is optional for a standard Kodi import.

### In the optional web/server application

Open the web application's settings and add a Kodi source/instance using the same connection details. The server itself must be able to reach Kodi on the local network. See the [server installation guide](installation.md) if necessary.

## Troubleshooting

- Make sure Kodi is running and its web server is enabled; the HTTP and WebSocket ports serve different purposes.
- If discovery fails, enter Kodi's local IP address directly. Check the iOS Local Network permission and whether guest Wi-Fi or a firewall isolates the devices.
- An HTTP 401 response usually means the Kodi username or password is incorrect.
- Do not expose Kodi's HTTP or WebSocket ports to the Internet. The application-control interfaces may be unauthenticated. Use a trusted local network or appropriately configured private VPN instead.
- A MariaDB/MyVideos connection is optional for advanced offline fallback; it is not necessary to start using Kodi JSON-RPC.

Official Kodi documentation: [Services / Control](https://kodi.wiki/view/Settings/Services/Control), [Web interface](https://kodi.wiki/view/Web_interface), [JSON-RPC API](https://kodi.wiki/view/JSON-RPC_API).
