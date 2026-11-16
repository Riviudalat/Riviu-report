import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { CalendarDaysIcon, CalendarRangeIcon, DownloadIcon, HandshakeIcon, Loader2Icon, SearchIcon, UserRoundIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Checkbox, Input } from '@/components/ui/misc';
import { PlatformIcon } from '@/components/common';
import { apiFetch, errorMessage, getJson } from '@/lib/api';
import { downloadBlob, filenameFromDisposition, formatNumber, normalizeVietnameseKey } from '@/lib/format';
import { buildExportFallbackFilename, DEFAULT_MIN_VIEWS, normalizeReportPartners, partnerLinkStatusText, reportFilterParams, type ReportPartner } from '@/lib/partners';
import { PLATFORM_KEYS, platformConfig, type PlatformKey } from '@/lib/platforms';
import {
  NO_POSTER, comparePosters, exportBody, monthLabel, monthRange, periodLabel, periodProblem, reportPartnersQuery, type ExportSubject, type ReportPeriod,
} from '@/lib/riviuData';
import { cn } from '@/lib/utils';
import { closeModal, notify, useApp } from '@/state/app';
import { monthsOf, selectRdPlatform } from '@/state/riviuData';

const SUBJECTS: Record<ExportSubject, { tab: string; noun: string; icon: ReactNode; path: string; key: 'partners' | 'posters' }> = {
  partner: { tab: 'Đối tác', noun: 'đối tác', icon: <HandshakeIcon />, path: '/riviu-data/report-partners', key: 'partners' },
  poster: { tab: 'Người đăng', noun: 'người đăng', icon: <UserRoundIcon />, path: '/riviu-data/report-posters', key: 'posters' },
};

type PeriodKind = ReportPeriod['kind'];

async function fetchReportNames(subject: ExportSubject, platform: PlatformKey, period: ReportPeriod, applyMinViews: boolean, minViews: number): Promise<{ list: ReportPartner[]; undated: number }> {
  const { path, key, noun } = SUBJECTS[subject];
  const { response, data } = await getJson<Record<string, unknown> & { error?: string }>(`${path}?${reportPartnersQuery(platform, period, applyMinViews, minViews)}`);
  if (!response.ok) throw new Error(data.error || `Không tải được danh sách ${noun}`);
  const list = normalizeReportPartners(data[key]);
  list.sort(subject === 'poster' ? (a, b) => comparePosters(a.name, b.name) : (a, b) => a.name.localeCompare(b.name, 'vi'));
  // A date-range report leaves out links without a NGÀY AIR; the server counts them.
  return { list, undated: Number(data.undated) || 0 };
}

export function ExportDialog() {
  const open = useApp(state => state.modal === 'export');
  return (
    <Dialog open={open} onOpenChange={value => !value && closeModal()}>
      <DialogContent className="max-w-2xl">{open && <ExportBody />}</DialogContent>
    </Dialog>
  );
}

/** A row of tabs inside the dialog (segmented control). */
function Segmented<T extends string>({ value, onChange, items, label, testId }: {
  value: T; onChange: (value: T) => void; items: Array<{ value: T; label: ReactNode }>; label: string; testId: string;
}) {
  return (
    <div className="grid auto-cols-fr grid-flow-col gap-1 rounded-lg bg-muted p-1" role="tablist" aria-label={label} data-testid={testId}>
      {items.map(item => {
        const selected = item.value === value;
        return (
          <button
            key={item.value}
            type="button"
            role="tab"
            aria-selected={selected}
            onClick={() => onChange(item.value)}
            className={cn(
              'flex h-8 min-w-0 items-center justify-center gap-2 whitespace-nowrap rounded-md px-3 text-sm font-medium transition [&_svg]:size-4',
              selected ? 'bg-card text-foreground shadow-sm ring-1 ring-border/70' : 'text-muted-foreground hover:bg-card/60 hover:text-foreground',
            )}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}

function ExportBody() {
  const platform = useApp(state => state.activePlatform);
  const thang = useApp(state => state.rd.thang[state.activePlatform]);
  const label = useApp(state => monthsOf(state).find(month => month.thang === thang)?.label) || monthLabel(thang);
  const [subject, setSubject] = useState<ExportSubject>('partner');
  const [periodKind, setPeriodKind] = useState<PeriodKind>('month');
  // The range starts as the shown month (up to today while it runs).
  const [range, setRange] = useState(() => monthRange(thang));
  const period: ReportPeriod = periodKind === 'month' ? { kind: 'month', thang } : { kind: 'range', from: range.from, to: range.to };
  const problem = periodProblem(period);
  const [search, setSearch] = useState('');
  // Each platform × subject tab keeps its own ticks while the dialog is open.
  const [selections, setSelections] = useState<Record<string, string[]>>({});
  const [applyMinViews, setApplyMinViews] = useState(true);
  const [minViewsInput, setMinViewsInput] = useState(String(DEFAULT_MIN_VIEWS));
  // The list reloads when the value is committed (blur / Enter), not per keystroke.
  const [minViewsCommitted, setMinViewsCommitted] = useState(String(DEFAULT_MIN_VIEWS));
  const [exporting, setExporting] = useState(false);
  const { minViews } = reportFilterParams(applyMinViews, minViewsCommitted);
  const { noun } = SUBJECTS[subject];
  const tabKey = `${platform}:${subject}`;
  const selected = useMemo(() => new Set(selections[tabKey] ?? []), [selections, tabKey]);

  const query = useQuery({
    queryKey: ['rd-report-names', subject, platform, period, applyMinViews, minViews],
    queryFn: () => fetchReportNames(subject, platform, period, applyMinViews, minViews),
    enabled: !problem,
    staleTime: 0,
  });
  const names = useMemo(() => query.data?.list ?? [], [query.data]);
  const undated = periodKind === 'range' ? query.data?.undated ?? 0 : 0;
  // A new list (filter or month) keeps only the ticked names it still has, in this tab only.
  useEffect(() => {
    if (!query.data) return;
    setSelections(previous => {
      const kept = (previous[tabKey] ?? []).filter(name => query.data.list.some(item => item.name === name));
      return kept.length === (previous[tabKey] ?? []).length ? previous : { ...previous, [tabKey]: kept };
    });
  }, [query.data, tabKey]);
  const visible = useMemo(() => {
    const needle = normalizeVietnameseKey(search);
    return needle ? names.filter(item => normalizeVietnameseKey(item.name).includes(needle)) : names;
  }, [names, search]);
  const chosen = names.filter(item => selected.has(item.name));
  const allVisibleSelected = visible.length > 0 && visible.every(item => selected.has(item.name));

  const update = (change: (next: Set<string>) => void) => setSelections(previous => {
    const next = new Set(previous[tabKey] ?? []);
    change(next);
    return { ...previous, [tabKey]: [...next] };
  });
  const toggle = (name: string, value: boolean) => update(next => { if (value) next.add(name); else next.delete(name); });
  const toggleVisible = () => update(next => {
    for (const item of visible) {
      if (allVisibleSelected) next.delete(item.name); else next.add(item.name);
    }
  });
  const switchTab = (change: () => void) => {
    setSearch('');
    change();
  };

  const exportReport = async () => {
    const picked = chosen.map(item => item.name);
    if (problem || !picked.length) return;
    setExporting(true);
    try {
      const response = await apiFetch('/riviu-data/export-report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(exportBody(platform, period, picked, applyMinViews, minViews, subject)),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || 'Không xuất được báo cáo');
      }
      const blob = await response.blob();
      const tag = period.kind === 'month' ? period.thang : `${period.from}_${period.to}`;
      const filename = filenameFromDisposition(response.headers.get('Content-Disposition') || '', buildExportFallbackFilename(picked, tag));
      downloadBlob(blob, filename);
      notify(`Đã xuất báo cáo: ${filename}`, 'success');
      closeModal();
    } catch (error) {
      notify(`Lỗi xuất báo cáo: ${errorMessage(error, '')}`, 'error');
    } finally {
      setExporting(false);
    }
  };

  return (
    <>
      <DialogHeader>
        <DialogTitle><DownloadIcon className="size-5 text-primary" /> Xuất báo cáo</DialogTitle>
        <DialogDescription>
          {periodKind === 'month' ? label || 'Chưa có tháng' : periodLabel(period)} • {query.isSuccess ? `${names.length} ${noun}` : `Chọn ${noun} để tải báo cáo Excel.`}
        </DialogDescription>
      </DialogHeader>
      <DialogBody className="flex flex-col gap-3">
        <div className="grid gap-2 sm:grid-cols-2">
          <Segmented
            label="Nền tảng"
            testId="export-platform"
            value={platform}
            onChange={key => switchTab(() => void selectRdPlatform(key))}
            items={PLATFORM_KEYS.map(key => ({ value: key, label: <><PlatformIcon platform={key} />{platformConfig(key).label}</> }))}
          />
          <Segmented
            label="Xuất theo"
            testId="export-subject"
            value={subject}
            onChange={value => switchTab(() => setSubject(value))}
            items={(Object.keys(SUBJECTS) as ExportSubject[]).map(key => ({ value: key, label: <>{SUBJECTS[key].icon}{SUBJECTS[key].tab}</> }))}
          />
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center" data-testid="export-period">
          <Segmented
            label="Khoảng báo cáo"
            testId="export-period-kind"
            value={periodKind}
            onChange={value => setPeriodKind(value)}
            items={[
              { value: 'month' as const, label: <><CalendarDaysIcon />Theo tháng</> },
              { value: 'range' as const, label: <><CalendarRangeIcon />Từ ngày – đến ngày</> },
            ]}
          />
          {periodKind === 'range' && (
            <div className="grid grid-cols-2 gap-2 sm:flex sm:items-center">
              <label className="flex min-w-0 items-center gap-1.5 text-sm">
                <span className="shrink-0 text-muted-foreground">Từ</span>
                <Input type="date" className="h-8 min-w-0" value={range.from} max={range.to || undefined}
                  onChange={event => setRange(previous => ({ ...previous, from: event.target.value }))} aria-label="Từ ngày" />
              </label>
              <label className="flex min-w-0 items-center gap-1.5 text-sm">
                <span className="shrink-0 text-muted-foreground">đến</span>
                <Input type="date" className="h-8 min-w-0" value={range.to} min={range.from || undefined}
                  onChange={event => setRange(previous => ({ ...previous, to: event.target.value }))} aria-label="Đến ngày" />
              </label>
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border bg-muted/40 px-3 py-2 text-sm">
          <label className="flex cursor-pointer select-none items-center gap-2">
            <Checkbox checked={applyMinViews} onCheckedChange={value => setApplyMinViews(value === true)} aria-label="Bỏ link dưới ngưỡng lượt xem" />
            Bỏ link dưới
          </label>
          <Input type="number" min={0} step={10} inputMode="numeric" className="h-8 w-24" value={minViewsInput} disabled={!applyMinViews}
            onChange={event => setMinViewsInput(event.target.value)}
            onBlur={() => setMinViewsCommitted(minViewsInput)}
            onKeyDown={event => { if (event.key === 'Enter') setMinViewsCommitted(minViewsInput); }}
            aria-label="Ngưỡng lượt xem" />
          <span className="text-muted-foreground">lượt xem</span>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative min-w-0 flex-1">
            <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input type="search" placeholder={`Tìm ${noun}…`} className="pl-8" value={search} onChange={event => setSearch(event.target.value)} aria-label={`Tìm ${noun}`} />
          </div>
          <Button variant="outline" size="sm" className="h-9" onClick={toggleVisible} disabled={!visible.length} data-testid="toggle-all">
            {allVisibleSelected ? 'Bỏ chọn' : 'Chọn tất cả'}
          </Button>
        </div>
        <div className="max-h-[min(42vh,24rem)] min-h-40 overflow-y-auto rounded-lg border p-1" data-testid="partner-list">
          {problem && periodKind === 'range' && <p className="p-6 text-center text-sm text-muted-foreground">{problem}</p>}
          {(periodKind === 'month' || !problem) && query.isPending && <p className="p-6 text-center text-sm text-muted-foreground">Đang tải danh sách {noun}…</p>}
          {query.isError && <p className="p-6 text-center text-sm text-destructive">{query.error.message}</p>}
          {query.isSuccess && visible.length === 0 && <p className="p-6 text-center text-sm text-muted-foreground">{names.length ? `Không tìm thấy ${noun} phù hợp.` : `${periodKind === 'month' ? 'Tháng này' : 'Khoảng ngày này'} chưa có ${noun} nào.`}</p>}
          {visible.map(item => {
            const note = partnerLinkStatusText(item);
            const exportable = Number(item.linkCount) > 0;
            return (
              <label key={item.name} className="flex cursor-pointer items-center gap-3 rounded-md px-2.5 py-2 text-sm hover:bg-muted/60">
                <Checkbox checked={selected.has(item.name)} onCheckedChange={value => toggle(item.name, value === true)} aria-label={item.name} />
                <span className={cn('min-w-0 flex-1 truncate font-medium', item.name === NO_POSTER && 'font-normal italic text-muted-foreground')}>{item.name}</span>
                {note
                  ? <span className="shrink-0 text-right text-xs text-muted-foreground">{note}</span>
                  : <span className={cn('shrink-0 rounded-full px-2 py-0.5 text-xs tabular-nums', exportable ? 'bg-accent text-accent-foreground' : 'bg-muted text-muted-foreground')}>{formatNumber(item.linkCount)} link</span>}
              </label>
            );
          })}
        </div>
        <p className="text-sm text-muted-foreground" data-testid="export-summary" aria-live="polite">
          {chosen.length === 0 ? `Chưa chọn ${noun} nào.` : chosen.length === 1 ? 'Sẽ tải 1 file Excel.' : `Đã chọn ${chosen.length}/${names.length} ${noun} — tải về 1 file .zip.`}
          {undated > 0 && <span className="block text-xs" data-testid="export-undated">{formatNumber(undated)} link chưa có ngày air nên không nằm trong khoảng ngày này.</span>}
        </p>
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" onClick={closeModal}>Đóng</Button>
        <Button disabled={!chosen.length || exporting || Boolean(problem)} onClick={() => void exportReport()} data-testid="export-report">
          {exporting ? <Loader2Icon className="animate-spin" /> : <DownloadIcon />}
          {exporting ? 'Đang xuất…' : 'Tải báo cáo'}
        </Button>
      </DialogFooter>
    </>
  );
}
