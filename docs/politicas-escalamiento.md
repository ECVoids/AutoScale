# Políticas de escalamiento — AutoScale

Documento de referencia para las políticas de creación y destrucción de instancias EC2 implementadas en `controller-asg/scaling_policy.py`.

---

## Principios de diseño

El ControllerASG sigue tres principios que guían todas las decisiones de escalamiento:

**Conservadurismo ante la oscillación.** Crear o destruir instancias tiene costo (tiempo de arranque, créditos AWS). El sistema exige que una condición se sostenga durante N evaluaciones consecutivas antes de actuar. Esto evita el *flapping* (escalar hacia arriba y hacia abajo en ciclos cortos).

**Mínimo garantizado.** El sistema nunca opera por debajo de `MIN_INSTANCES`. Esta regla tiene la mayor prioridad y se evalúa antes que cualquier otra.

**Cooldown obligatorio.** Después de un scale-up o scale-down se activa un período de enfriamiento durante el cual no se ejecuta otra acción del mismo tipo, aunque la política lo pida.

---

## Parámetros de configuración

Todos los umbrales son ajustables vía variables de entorno o directamente en `config.json`. Los valores por defecto están calibrados para cuentas AWS Academy con límite de 5 instancias.

| Parámetro | Default | Descripción |
|---|---|---|
| `MIN_INSTANCES` | `2` | Número mínimo de instancias siempre activas |
| `MAX_INSTANCES` | `5` | Límite máximo (restricción AWS Academy) |
| `EVAL_INTERVAL` | `30s` | Intervalo entre ciclos de evaluación |
| `COOLDOWN_UP` | `120s` | Espera mínima entre dos scale-ups consecutivos |
| `COOLDOWN_DOWN` | `180s` | Espera mínima entre dos scale-downs consecutivos |
| `SCALE_UP_CPU_THRESHOLD` | `70%` | CPU promedio que dispara scale-up |
| `SCALE_UP_CONFIRM_ROUNDS` | `2` | Evaluaciones consecutivas requeridas |
| `SCALE_DOWN_CPU_THRESHOLD` | `30%` | CPU promedio que dispara scale-down |
| `SCALE_DOWN_CONFIRM_ROUNDS` | `3` | Evaluaciones consecutivas requeridas |
| `UNREACHABLE_RATIO_THRESHOLD` | `0.5` | Fracción de instancias UNREACHABLE que fuerza scale-up |

---

## Estados de instancia

Cada instancia en el registro tiene uno de cuatro estados posibles, asignados en `instance_registry.py`:

| Estado | Condición | Descripción |
|---|---|---|
| `HEALTHY` | `cpu < 60%` | Instancia operativa con carga normal |
| `DEGRADED` | `60% ≤ cpu < 85%` | Carga alta pero aún funcional |
| `CRITICAL` | `cpu ≥ 85%` | Sobrecargada; candidata a disparar scale-up |
| `UNREACHABLE` | 3+ fallos de Ping consecutivos | No responde; no se usa para scale-down |

---

## Política de creación (scale-up)

Se evalúan tres reglas en orden de prioridad. La primera que aplica gana.

### Regla 0 — Mínimo garantizado

```
SI total_instancias < MIN_INSTANCES → scale_up inmediato
```

No requiere confirmación ni respeta cooldown. Asegura que el sistema siempre tenga al menos dos instancias activas, incluso tras una terminación inesperada.

### Regla 1 — Reemplazo por instancias inalcanzables

```
SI (unreachable / total) ≥ UNREACHABLE_RATIO_THRESHOLD → scale_up inmediato
```

Si la mitad o más de las instancias dejan de responder, el sistema asume una falla sistémica y lanza nuevas instancias sin esperar confirmación. El cooldown no aplica a esta regla.

### Regla 2 — Carga alta sostenida

```
SI avg_cpu ≥ SCALE_UP_CPU_THRESHOLD
   Y total < MAX_INSTANCES
   Y condición se cumple SCALE_UP_CONFIRM_ROUNDS veces seguidas
→ scale_up
```

El contador `up_streak` se incrementa en cada evaluación donde se cumple la condición. Si la carga baja antes de llegar a `SCALE_UP_CONFIRM_ROUNDS`, el contador se reinicia a cero.

**Ejemplo de secuencia:**

```
t=0  avg_cpu=72%  → up_streak=1  (esperando confirmación)
t=30 avg_cpu=75%  → up_streak=2  → SCALE_UP ejecutado
t=30 cooldown activo por 120s
```

---

## Política de destrucción (scale-down)

### Regla 3 — Carga baja sostenida

```
SI avg_cpu ≤ SCALE_DOWN_CPU_THRESHOLD
   Y total > MIN_INSTANCES
   Y condición se cumple SCALE_DOWN_CONFIRM_ROUNDS veces seguidas
→ scale_down
```

El umbral de confirmación es mayor que el de scale-up (3 vs 2) porque destruir una instancia en uso tiene mayor impacto que lanzar una nueva.

**Ejemplo de secuencia:**

```
t=0   avg_cpu=25%  → down_streak=1
t=30  avg_cpu=28%  → down_streak=2
t=60  avg_cpu=22%  → down_streak=3  → SCALE_DOWN ejecutado
t=60  cooldown activo por 180s
```

---

## Selección de candidato para scale-down

Cuando se decide destruir una instancia, `pick_candidate_for_removal()` elige la menos costosa de eliminar:

1. Solo considera instancias en estado `HEALTHY` o `DEGRADED`. Las `UNREACHABLE` se excluyen (ya están fallando y no deben ser el objetivo de una terminación controlada).
2. Ordena por `cpu_percent` ascendente (menos cargada primero).
3. En caso de empate de CPU, prefiere la de mayor `uptime_seconds` (más antigua).

Antes de terminar la instancia elegida, el ControllerASG llama `force_deregister()` vía gRPC para que el MonitorC se desregistre limpiamente del MonitorS.

---

## Diagrama de decisión

```
Inicio de ciclo de evaluación
         │
         ▼
  total < MIN?  ──sí──▶ scale_up (inmediato, sin cooldown)
         │no
         ▼
  unreachable_ratio ≥ 0.5?  ──sí──▶ scale_up (inmediato)
         │no
         ▼
  avg_cpu ≥ 70% y total < MAX?
  ──sí──▶ up_streak++
            up_streak ≥ 2?  ──sí──▶ scale_up (con cooldown)
         │no                    │no → no_change
         ▼
  avg_cpu ≤ 30% y total > MIN?
  ──sí──▶ down_streak++
            down_streak ≥ 3?  ──sí──▶ scale_down (con cooldown)
         │no                     │no → no_change
         ▼
      no_change
```

---

## Supuestos y limitaciones

- El sistema usa CPU promedio del cluster como única métrica. En producción se podrían agregar métricas de latencia HTTP, longitud de cola, o memoria.
- Las cuentas AWS Academy tienen límite estricto de 5 instancias simultáneas. `MAX_INSTANCES=5` no debe aumentarse sin verificar los límites de la cuenta.
- El tiempo de arranque de una instancia EC2 (1-3 minutos) no está incluido en el cooldown. Durante ese período el MonitorS puede ver un `total` incorrecto hasta que el nuevo MonitorC complete el registro.
- El sistema no implementa scale-down de múltiples instancias en un solo ciclo. Cada decisión termina como máximo una instancia.
