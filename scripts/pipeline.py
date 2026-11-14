"""Cenário sintético acadêmico do SYS Sense. Execute a partir de qualquer pasta."""
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from statistics import mean
import csv
import hashlib
import json
import math
import random
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
OUT = ROOT / 'results'
SEED = 20261026
N = 8640
FIELDS = ['data_hora', 'dispositivo', 'temperatura_ambiente_c', 'umidade_relativa_pct',
          'uso_cpu_pct', 'uso_memoria_pct', 'tempo_atividade']
MEASURES = ['temperatura_ambiente_c', 'umidade_relativa_pct', 'uso_cpu_pct', 'uso_memoria_pct']


def save_csv(path, rows, fields):
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def synthetic_series(rng, target, amplitude):
    series = [amplitude * math.sin(i / 310) + rng.uniform(-amplitude / 4, amplitude / 4) for i in range(N)]
    offset = mean(series)
    return [round(target + v - offset, 4) for v in series]


def generate_source():
    rng = random.Random(SEED)
    # O patamar de calor é imposto na simulação para demonstrar a análise.
    # Não foi descoberto em observações de um ambiente real.
    temp = [25 + .35 * math.sin(i * 2 * math.pi / 360) + rng.uniform(-.08, .08) for i in range(N)]
    for i in range(14 * 360, 18 * 360):
        temp[i] += 6.5
    correction = 26.09 - mean(temp[12:])
    temp = [round(v + correction, 4) for v in temp]
    humidity = synthetic_series(rng, 52.05, 1.3)
    cpu = synthetic_series(rng, 35.43, 11)
    memory = synthetic_series(rng, 58.20, 4)
    start = datetime(2026, 10, 26)
    rows = []
    for i in range(N):
        stamp = (start + timedelta(seconds=10 * i)).isoformat(sep=' ')
        rows.append(dict(zip(FIELDS, [stamp, 'ESP32', '' if i < 12 else temp[i], humidity[i], '', '', ''])))
        rows.append(dict(zip(FIELDS, [stamp, 'NUC7080', 0, 0, cpu[i], memory[i], str(timedelta(seconds=86400 + i * 10))])))
    rows.extend(dict(rows[i]) for i in range(8))
    save_csv(DATA / 'registros_origem.csv', rows, FIELDS)


def clean_source():
    with (DATA / 'registros_origem.csv').open(newline='', encoding='utf-8') as f:
        raw = list(csv.DictReader(f))
    seen = set()
    cleaned = []
    duplicates = 0
    for row in raw:
        datetime.strptime(row['data_hora'], '%Y-%m-%d %H:%M:%S')
        if row['dispositivo'] not in {'ESP32', 'NUC7080'}:
            raise ValueError('Origem desconhecida')
        key = (row['dispositivo'], row['data_hora'])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        # Zero ambiental no canal computacional representa campo não aplicável.
        if row['dispositivo'] == 'NUC7080':
            row['temperatura_ambiente_c'] = ''
            row['umidade_relativa_pct'] = ''
        for field in MEASURES:
            if row[field] != '':
                row[field] = float(row[field])
                if not math.isfinite(row[field]):
                    raise ValueError('Medida não finita')
                if field.endswith('_pct') and not 0 <= row[field] <= 100:
                    raise ValueError('Percentual fora do intervalo de 0 a 100')
        cleaned.append(row)
    save_csv(DATA / 'registros_tratados.csv', cleaned, FIELDS)
    return raw, cleaned, duplicates


def integrate(cleaned):
    env = {r['data_hora']: r for r in cleaned if r['dispositivo'] == 'ESP32'}
    server = {r['data_hora']: r for r in cleaned if r['dispositivo'] == 'NUC7080'}
    if env.keys() != server.keys():
        raise ValueError('Os canais não possuem os mesmos horários')
    records = []
    for stamp in sorted(env):
        a, b = env[stamp], server[stamp]
        temp = a['temperatura_ambiente_c']
        records.append({'data_hora': stamp, 'servidor': 'NUC7080',
                        **{k: a[k] if k in MEASURES[:2] else b[k] for k in MEASURES},
                        'tempo_atividade': b['tempo_atividade'],
                        'temperatura_acima_30_c': '' if temp == '' else int(temp > 30)})
    save_csv(DATA / 'monitoramento.csv', records, list(records[0]))
    return records


def summarize(records):
    groups = defaultdict(list)
    for r in records:
        groups[r['data_hora'][:13]].append(r)
    summary = []
    for hour, rows in sorted(groups.items()):
        item = {'hora': hour + ':00:00'}
        for key in MEASURES:
            values = [r[key] for r in rows if r[key] != '']
            item.update({key + '_media': round(mean(values), 6),
                         key + '_min': min(values), key + '_max': max(values),
                         key + '_validos': len(values)})
        item['leituras_acima_30_c'] = sum(r['temperatura_acima_30_c'] == 1 for r in rows)
        summary.append(item)
    save_csv(DATA / 'resumo_horario.csv', summary, list(summary[0]))
    return summary


def database(cleaned, records):
    # Substitui somente as tabelas de saída deste protótipo local.
    with sqlite3.connect(DATA / 'monitoramento.db') as con:
        con.execute('DROP TABLE IF EXISTS registros')
        con.execute('DROP TABLE IF EXISTS monitoramento')
        con.execute('CREATE TABLE registros (data_hora TEXT NOT NULL, dispositivo TEXT NOT NULL, temperatura_ambiente_c REAL, umidade_relativa_pct REAL, uso_cpu_pct REAL, uso_memoria_pct REAL, tempo_atividade TEXT, PRIMARY KEY(dispositivo, data_hora))')
        con.execute('CREATE TABLE monitoramento (data_hora TEXT PRIMARY KEY, servidor TEXT NOT NULL, temperatura_ambiente_c REAL, umidade_relativa_pct REAL, uso_cpu_pct REAL, uso_memoria_pct REAL, tempo_atividade TEXT, temperatura_acima_30_c INTEGER)')
        def values(row):
            return tuple(None if v == '' else v for v in row.values())
        con.executemany('INSERT INTO registros VALUES (?,?,?,?,?,?,?)', [values(r) for r in cleaned])
        con.executemany('INSERT INTO monitoramento VALUES (?,?,?,?,?,?,?,?)', [values(r) for r in records])
        assert con.execute('SELECT COUNT(*) FROM registros').fetchone()[0] == 17280
        assert con.execute('SELECT COUNT(*) FROM monitoramento WHERE temperatura_acima_30_c = 1').fetchone()[0] == 1440
        assert con.execute('SELECT COUNT(*) FROM monitoramento WHERE temperatura_ambiente_c IS NULL AND temperatura_acima_30_c IS NULL').fetchone()[0] == 12
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def dashboard(summary, metrics):
    from PIL import Image, ImageDraw, ImageFont
    def font(size):
        for p in ['C:/Windows/Fonts/arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        return ImageFont.load_default()
    im = Image.new('RGB', (1800, 1120), '#f5f7fa')
    d = ImageDraw.Draw(im)
    d.text((55, 35), 'SYS Sense  Monitoramento de servidores', fill='#172c40', font=font(42))
    d.text((55, 92), 'SIMULAÇÃO ACADÊMICA | 26/10/2026 | Médias horárias | Horário de São Paulo', fill='#4c5e6d', font=font(25))
    specs = [('Temperatura ambiente', '°C', 20, 35), ('Umidade relativa', '%', 40, 65),
             ('Utilização do processador', '%', 0, 100), ('Utilização da memória', '%', 0, 100)]
    for n, ((title, unit, low, high), field) in enumerate(zip(specs, MEASURES)):
        x = 55 + (n % 2) * 880
        y = 165 + (n // 2) * 410
        d.rounded_rectangle((x, y, x+825, y+375), radius=12, fill='white', outline='#d7dee5', width=2)
        d.text((x+25, y+20), title + ' (' + unit + ')', fill='#172c40', font=font(28))
        left, top, width, height = x+85, y+85, 690, 220
        def yy(v): return top+height-(v-low)/(high-low)*height
        for v in [low, (low+high)/2, high]:
            d.line((left, yy(v), left+width, yy(v)), fill='#d7dee5', width=1)
            d.text((x+13, yy(v)-12), f'{v:g}', fill='#455465', font=font(20))
        points = [(left+i*width/23, yy(r[field+'_media'])) for i,r in enumerate(summary)]
        if n == 0:
            for off in range(0, width, 24):
                d.line((left+off, yy(30), left+min(off+13,width), yy(30)), fill='#c44336', width=3)
            d.text((left+width-100, yy(30)-28), '30 °C', fill='#c44336', font=font(22))
        d.line(points, fill='#21688f', width=4)
        for px,py in points: d.ellipse((px-4,py-4,px+4,py+4), fill='#21688f')
        for i in [0,6,12,18,23]:
            d.text((left+i*width/23-10, top+height+14), f'{i:02d}', fill='#455465', font=font(20))
        d.text((left+width/2-30, top+height+44), 'Hora', fill='#455465', font=font(20))
    d.text((55, 1010), '1.440 de 8.628 temperaturas válidas > 30 °C (16,69%) | Completude térmica 99,86%', fill='#172c40', font=font(28))
    d.text((55, 1054), 'O patamar das 14h às 17h59min50s foi definido na geração dos dados; não representa evidência real.', fill='#4c5e6d', font=font(23))
    im.save(OUT / 'dashboard.png')


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    generate_source()
    raw, cleaned, duplicates = clean_source()
    records = integrate(cleaned)
    summary = summarize(records)
    valid = [r['temperatura_ambiente_c'] for r in records if r['temperatura_ambiente_c'] != '']
    hot = [r for r in records if r['temperatura_acima_30_c'] == 1]
    metrics = {'natureza': 'Dados sintéticos para demonstração acadêmica; janela quente imposta na geração',
               'semente': SEED, 'fuso_referencia': 'America/Sao_Paulo', 'intervalo_segundos': 10,
               'linhas_origem': len(raw), 'duplicatas_removidas': duplicates,
               'registros_tratados': len(cleaned), 'horarios_consolidados': len(records),
               'temperaturas_validas': len(valid), 'temperaturas_ausentes': N-len(valid),
               'leituras_acima_30_c': len(hot), 'percentual_acima_30_c': len(hot)/len(valid)*100,
               'completude_temperatura_pct': len(valid)/N*100,
               'primeira_leitura_acima': hot[0]['data_hora'], 'ultima_leitura_acima': hot[-1]['data_hora'],
               'medias': {k: mean([r[k] for r in records if r[k] != '']) for k in MEASURES}}
    assert (len(raw), duplicates, len(cleaned), len(records), len(summary), len(valid), len(hot)) == (17288,8,17280,8640,24,8628,1440)
    assert all(int(r['data_hora'][11:13]) in range(14,18) for r in hot)
    assert len(set(r['data_hora'] for r in records)) == N
    times = [datetime.fromisoformat(r['data_hora']) for r in records]
    assert all((b-a).total_seconds() == 10 for a,b in zip(times,times[1:]))
    assert sum(r['temperatura_ambiente_c_validos'] for r in summary) == len(valid)
    assert sum(r['leituras_acima_30_c'] for r in summary) == len(hot)
    assert summary[0]['temperatura_ambiente_c_validos'] == 348
    assert [round(metrics['medias'][k],2) for k in MEASURES] == [26.09,52.05,35.43,58.20]
    database(cleaned, records)
    metrics['validacao'] = 'APROVADA: contagens, horários, regra térmica, médias, agregações e SQLite'
    (OUT/'metricas.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    dashboard(summary, metrics)
    paths = list(DATA.glob('*.csv')) + [OUT/'dashboard.png', OUT/'metricas.json']
    hashes = {str(p.relative_to(ROOT)).replace('\\','/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    (OUT/'sha256.json').write_text(json.dumps(hashes,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
