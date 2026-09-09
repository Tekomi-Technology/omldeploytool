# Tekomi overlay image: pinned upstream base + prod fix files only.
#
# Por que existe: el rebuild completo (oml_manage.sh rebuild) esta roto
# upstream en este commit (pnpm install falla por ERR_PNPM_PATCH_FAILED
# en webui). Este overlay evita reconstruir todo: parte de la imagen
# pinneada y solo copia los .py modificados (auditables abajo).
#
# Refrescar: agregar COPY por cada .py nuevo de la rama tekomi-sipdev:
#   git diff <base-pin> tekomi-sipdev --stat  # en el fork ominicontacto
#
# Build (desde omldeploytool/docker-compose/prod-env):
#   ./build-tekomi.sh
ARG BASE_IMG=docker.io/omnileads/omlapp:20260824-7aec5d3b
FROM ${BASE_IMG}
COPY ominicontacto_app/services/sip_devices.py /opt/omnileads/ominicontacto/ominicontacto_app/services/sip_devices.py
COPY ominicontacto_app/services/asterisk/redis_database.py /opt/omnileads/ominicontacto/ominicontacto_app/services/asterisk/redis_database.py
COPY ominicontacto_app/services/asterisk/supervisor_activity.py /opt/omnileads/ominicontacto/ominicontacto_app/services/asterisk/supervisor_activity.py
COPY ominicontacto_app/management/commands/verificar_families_redis.py /opt/omnileads/ominicontacto/ominicontacto_app/management/commands/verificar_families_redis.py
COPY ominicontacto_app/management/commands/cron_tick_5.py /opt/omnileads/ominicontacto/ominicontacto_app/management/commands/cron_tick_5.py
COPY ominicontacto_app/forms/base.py /opt/omnileads/ominicontacto/ominicontacto_app/forms/base.py
COPY ominicontacto_app/views_user_profiles.py /opt/omnileads/ominicontacto/ominicontacto_app/views_user_profiles.py
COPY ominicontacto_app/asterisk_config_generador_de_partes.py /opt/omnileads/ominicontacto/ominicontacto_app/asterisk_config_generador_de_partes.py
COPY tekomi_crm_bridge_adapter /opt/omnileads/ominicontacto/tekomi_crm_bridge_adapter
COPY ominicontacto/urls.py /opt/omnileads/ominicontacto/ominicontacto/urls.py
COPY ominicontacto/settings/defaults.py /opt/omnileads/ominicontacto/ominicontacto/settings/defaults.py
COPY ominicontacto/settings/oml_settings_local.py /opt/omnileads/ominicontacto/ominicontacto/settings/oml_settings_local.py
COPY ominicontacto_app/templates/agente/base_agente.html /opt/omnileads/ominicontacto/ominicontacto_app/templates/agente/base_agente.html
RUN python3 -m compileall -q \
  /opt/omnileads/ominicontacto/ominicontacto_app/services/sip_devices.py \
  /opt/omnileads/ominicontacto/ominicontacto_app/services/asterisk/redis_database.py \
  /opt/omnileads/ominicontacto/ominicontacto_app/services/asterisk/supervisor_activity.py \
  /opt/omnileads/ominicontacto/ominicontacto_app/management/commands/verificar_families_redis.py \
  /opt/omnileads/ominicontacto/ominicontacto_app/forms/base.py \
  /opt/omnileads/ominicontacto/ominicontacto_app/views_user_profiles.py \
  /opt/omnileads/ominicontacto/ominicontacto_app/asterisk_config_generador_de_partes.py \
  /opt/omnileads/ominicontacto/tekomi_crm_bridge_adapter/views.py \
  /opt/omnileads/ominicontacto/ominicontacto/urls.py \
  /opt/omnileads/ominicontacto/ominicontacto/settings/defaults.py \
  /opt/omnileads/ominicontacto/ominicontacto/settings/oml_settings_local.py \
  && echo OVERLAY-OK
