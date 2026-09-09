#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy_pi.sh — Despliega main.py al kiosco (Raspberry Pi) y lo reinicia.
#
# Uso (Git Bash en el PC):   bash deploy_pi.sh
#
# Qué hace:
#   1. Descubre solo el usuario SSH probando candidatos (con las 2 llaves).
#   2. Copia SOLO main.py a /home/<usuario>/PVLockes/main.py (no toca git ni
#      otros archivos a medio terminar).
#   3. Reinicia el kiosco para que cargue la pantalla nueva y re-sincronice
#      el nombre "Edificio Holanda 1064" (ya escrito en Firestore).
# ---------------------------------------------------------------------------
set -u

IP="192.168.23.30"
LOCAL_MAIN="/c/Users/cmedi/Documents/Antigravity/PVLocker/main.py"
KEYS=("$HOME/.ssh/id_ed25519" "$HOME/.ssh/pv_deploy")
# El hostname de la Pi es "LockersHolanda" -> se agregan candidatos temáticos.
USUARIOS=(pi lockersholanda holanda lockers kiosk kiosko porteria admin cmedina)

SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 -o LogLevel=ERROR"

echo "==> Buscando usuario y llave que entren a $IP ..."
KEY_OK=""; USER_OK=""
for KEY in "${KEYS[@]}"; do
  [ -f "$KEY" ] || continue
  for U in "${USUARIOS[@]}"; do
    if ssh $SSH_OPTS -i "$KEY" "$U@$IP" true 2>/dev/null; then
      KEY_OK="$KEY"; USER_OK="$U"; break 2
    fi
  done
done

if [ -z "$USER_OK" ]; then
  echo "!! No pude entrar con ninguna combinación conocida."
  echo "   Prueba a mano:  ssh -i ~/.ssh/id_ed25519 TU_USUARIO@$IP"
  echo "   y avísame qué usuario/llave funcionó."
  exit 1
fi
echo "    OK -> usuario=$USER_OK   llave=$(basename "$KEY_OK")"

echo "==> Copiando main.py ..."
if ! scp $SSH_OPTS -i "$KEY_OK" "$LOCAL_MAIN" "$USER_OK@$IP:/home/$USER_OK/PVLockes/main.py"; then
  echo "!! Falló la copia. ¿La ruta en la Pi es /home/$USER_OK/PVLockes ?"
  exit 1
fi
echo "    main.py copiado."

echo "==> Reiniciando el kiosco ..."
ssh $SSH_OPTS -i "$KEY_OK" "$USER_OK@$IP" '
  if systemctl list-unit-files 2>/dev/null | grep -qi "porteria"; then
    SVC=$(systemctl list-unit-files | grep -i porteria | head -1 | awk "{print \$1}")
    echo "    Reiniciando servicio: $SVC"
    sudo systemctl restart "$SVC" && echo "    Servicio reiniciado."
  else
    echo "    No hay servicio systemd; reinicio el proceso directo."
    pkill -f main.py 2>/dev/null
    echo "    Proceso detenido. Si no reinicia solo (autostart de labwc),"
    echo "    reinicia la Pi:  sudo reboot"
  fi
'
echo "==> Listo. Verifica en la pantalla del kiosco:"
echo "    - Solo el botón 'Dejar Encomienda' (más grande)"
echo "    - Título: Edificio Holanda 1064"
