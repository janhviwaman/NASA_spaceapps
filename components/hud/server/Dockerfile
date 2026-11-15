# Optional base-station server for the HUD. Build with the HUD folder as context:
#   docker build -f server/Dockerfile -t mars-hud-base .   (run from components/hud)
FROM node:20-alpine
WORKDIR /app
COPY . .
WORKDIR /app/server
RUN npm ci --omit=dev
ENV PORT=8443
EXPOSE 8443
CMD ["node", "server.js"]
