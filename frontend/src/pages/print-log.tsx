import { useId, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, Box, Pencil } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input, inputClasses } from "@/components/ui/input";
import { PageContainer } from "@/components/ui/page-container";
import { PageHeader } from "@/components/ui/page-header";
import { ProtectedThumbnail } from "@/components/protected-thumbnail";
import { annotatePrinterJob, listFleetQueue, listModels, type PrintJobCorrection } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { formatCurrency } from "@/lib/currency";
import { formatDuration, formatGrams, formatNumber, formatPercent } from "@/lib/format";
import { useUiLocale } from "@/lib/i18n";
import { currentLocale, uiText } from "@/lib/locale";
import { toast } from "@/lib/toast";
import type { ModelListItem, PrintJobRead } from "@/types";

const HISTORY_PAGE = 100;
const FINISHED = new Set(["completed", "failed", "cancelled"]);
const PRICE_KEY = "printstash.printLog.pricePerKg";
// The four IFS spools' price; a field on the page changes it per browser.
const DEFAULT_PRICE_PER_KG = 10;
// Spool names are the colour of the filament (see the printer app's charging).
const SPOOL_COLORS = new Map([
  ["black", "#1f2937"],
  ["white", "#f8fafc"],
  ["blue", "#2563eb"],
  ["red", "#dc2626"],
]);

const spoolColor = (name: string) => SPOOL_COLORS.get(name.toLowerCase()) ?? "transparent";

async function allFinishedJobs(): Promise<PrintJobRead[]> {
  const jobs: PrintJobRead[] = [];
  for (let offset = 0; ; offset += HISTORY_PAGE) {
    const page = await listFleetQueue(HISTORY_PAGE, offset);
    jobs.push(...page.filter((job) => FINISHED.has(job.state)));
    if (page.length < HISTORY_PAGE) return jobs;
  }
}

function readPrice(): number {
  try {
    const saved = Number(localStorage.getItem(PRICE_KEY));
    return saved > 0 ? saved : DEFAULT_PRICE_PER_KG;
  } catch {
    return DEFAULT_PRICE_PER_KG;
  }
}

const endedAt = (job: PrintJobRead) => job.finished_at ?? job.started_at ?? job.created_at;

function SpoolDot({ name }: { name: string | null | undefined }) {
  if (!name) return <span className="text-muted-foreground">-</span>;
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="h-2.5 w-2.5 shrink-0 rounded-full border border-muted-foreground/50"
        style={{ background: spoolColor(name) }}
      />
      {name}
    </span>
  );
}

/** "Name, folder": model names repeat across folders, so the folder tells them apart. */
const modelLabel = (model: ModelListItem) =>
  model.collection_label ? `${model.name}, ${model.collection_label}` : model.name;

/** `datetime-local` wants local wall time without a zone. */
function toLocalInput(iso: string): string {
  const at = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}T${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

const STATES = [
  ["completed", "Completed"],
  ["failed", "Failed"],
  ["cancelled", "Cancelled"],
] as const;

function PrintEditor({
  job,
  models,
  people,
  spools,
  onDone,
}: {
  job: PrintJobRead;
  models: ModelListItem[];
  people: string[];
  spools: string[];
  onDone: (saved: boolean) => void;
}) {
  const ids = useId();
  const byLabel = useMemo(() => new Map(models.map((m) => [modelLabel(m), m.id])), [models]);
  const current = models.find((m) => m.id === job.model_id);
  const seconds = job.actual_duration_s ?? 0;
  const [form, setForm] = useState({
    model: current ? modelLabel(current) : "",
    recipient: job.recipient ?? "",
    notes: job.notes ?? "",
    spool: job.spool_name ?? "",
    grams: job.filament_used_g == null ? "" : String(job.filament_used_g),
    hours: job.actual_duration_s == null ? "" : String(Math.floor(seconds / 3600)),
    minutes: job.actual_duration_s == null ? "" : String(Math.round((seconds % 3600) / 60)),
    finished: toLocalInput(endedAt(job)),
    state: job.state,
  });
  const [saving, setSaving] = useState(false);
  const set = (key: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm({ ...form, [key]: e.target.value });
  const modelId = byLabel.get(form.model);
  const modelUnknown = form.model !== "" && modelId === undefined;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (job.printer_id == null || modelUnknown) return;
    const payload: PrintJobCorrection = {
      recipient: form.recipient,
      notes: form.notes,
      spool_name: form.spool,
      filament_used_g: form.grams === "" ? null : Number(form.grams),
      actual_duration_s:
        form.hours === "" && form.minutes === ""
          ? null
          : Number(form.hours || 0) * 3600 + Number(form.minutes || 0) * 60,
    };
    if (form.finished !== toLocalInput(endedAt(job))) {
      payload.finished_at = new Date(form.finished).toISOString();
    }
    const state = STATES.find(([value]) => value === form.state)?.[0];
    if (state && state !== job.state) payload.state = state;
    if (modelId !== undefined && modelId !== job.model_id) payload.model_id = modelId;
    setSaving(true);
    try {
      await annotatePrinterJob(job.printer_id, job.id, payload);
      onDone(true);
    } catch (err) {
      toast.error(err);
    } finally {
      setSaving(false);
    }
  }

  const field = (label: string, input: React.ReactNode, wide = false) => (
    <label
      className={`flex flex-col gap-1 text-xs text-muted-foreground ${wide ? "col-span-2" : ""}`}
    >
      {label}
      {input}
    </label>
  );

  return (
    <form
      onSubmit={save}
      onKeyDown={(e) => {
        if (e.key === "Escape") onDone(false);
      }}
      className="grid grid-cols-2 gap-3 border-t border-border bg-muted/40 px-4 py-3 sm:grid-cols-4"
    >
      {field(
        uiText("Model"),
        <Input
          list={`${ids}-models`}
          value={form.model}
          onChange={set("model")}
          aria-invalid={modelUnknown}
          className={`h-8 ${modelUnknown ? "border-destructive" : ""}`}
        />,
        true,
      )}
      {field(
        uiText("For"),
        <Input
          list={`${ids}-people`}
          value={form.recipient}
          onChange={set("recipient")}
          maxLength={64}
          className="h-8"
        />,
      )}
      {field(
        uiText("Spool"),
        <Input
          list={`${ids}-spools`}
          value={form.spool}
          onChange={set("spool")}
          maxLength={64}
          className="h-8"
        />,
      )}
      {field(
        uiText("Grams"),
        <Input
          type="number"
          min={0}
          step={0.01}
          value={form.grams}
          onChange={set("grams")}
          className="h-8"
        />,
      )}
      {field(
        uiText("Hours"),
        <Input
          type="number"
          min={0}
          step={1}
          value={form.hours}
          onChange={set("hours")}
          className="h-8"
        />,
      )}
      {field(
        uiText("Minutes"),
        <Input
          type="number"
          min={0}
          max={59}
          step={1}
          value={form.minutes}
          onChange={set("minutes")}
          className="h-8"
        />,
      )}
      {field(
        uiText("Result"),
        <select value={form.state} onChange={set("state")} className={`${inputClasses} h-8 py-0`}>
          {STATES.map(([value, label]) => (
            <option key={value} value={value}>
              {uiText(label)}
            </option>
          ))}
        </select>,
      )}
      {field(
        uiText("Finished"),
        <Input
          type="datetime-local"
          required
          value={form.finished}
          onChange={set("finished")}
          className="h-8"
        />,
        true,
      )}
      {field(
        uiText("Notes"),
        <Input value={form.notes} onChange={set("notes")} maxLength={4096} className="h-8" />,
        true,
      )}
      <div className="col-span-2 flex items-end gap-2 sm:col-span-4">
        <Button type="submit" size="xs" disabled={saving || modelUnknown}>
          {uiText("Save")}
        </Button>
        <Button type="button" size="xs" variant="ghost" onClick={() => onDone(false)}>
          {uiText("Cancel")}
        </Button>
      </div>
      <datalist id={`${ids}-models`}>
        {models.map((m) => (
          <option key={m.id} value={modelLabel(m)} />
        ))}
      </datalist>
      <datalist id={`${ids}-people`}>
        {people.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
      <datalist id={`${ids}-spools`}>
        {spools.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
    </form>
  );
}

export default function PrintLogPage() {
  useUiLocale();
  const { user } = useAuth();
  const [person, setPerson] = useState<string | null>(null);
  const [thisMonth, setThisMonth] = useState(false);
  const [price, setPrice] = useState(readPrice);
  const [editing, setEditing] = useState<number | null>(null);

  const jobs = useQuery({ queryKey: ["print-log", "jobs"], queryFn: allFinishedJobs });
  // ponytail: one page of 500 models covers this library; past that, unknown ids show the filename
  const models = useQuery({
    queryKey: ["print-log", "models"],
    queryFn: () => listModels({ limit: 500 }),
  });
  const byModel = useMemo(
    () => new Map<number, ModelListItem>((models.data ?? []).map((m) => [m.id, m])),
    [models.data],
  );

  const people = useMemo(() => {
    const counts = new Map<string, number>();
    for (const job of jobs.data ?? []) {
      if (job.recipient) counts.set(job.recipient, (counts.get(job.recipient) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([name]) => name);
  }, [jobs.data]);

  const spools = useMemo(
    () => [
      ...new Set([...SPOOL_COLORS.keys(), ...(jobs.data ?? []).flatMap((j) => j.spool_name ?? [])]),
    ],
    [jobs.data],
  );

  const shown = useMemo(() => {
    const now = new Date();
    return (jobs.data ?? [])
      .filter((job) => person === null || job.recipient === person)
      .filter((job) => {
        if (!thisMonth) return true;
        const at = new Date(endedAt(job));
        return at.getFullYear() === now.getFullYear() && at.getMonth() === now.getMonth();
      })
      .sort((a, b) => endedAt(b).localeCompare(endedAt(a)));
  }, [jobs.data, person, thisMonth]);

  const days = useMemo(() => {
    const groups: { label: string; jobs: PrintJobRead[] }[] = [];
    for (const job of shown) {
      const label = new Date(endedAt(job)).toLocaleDateString(currentLocale(), {
        weekday: "short",
        day: "numeric",
        month: "short",
        year: "numeric",
      });
      if (groups.at(-1)?.label !== label) groups.push({ label, jobs: [] });
      groups.at(-1)!.jobs.push(job);
    }
    return groups;
  }, [shown]);

  const grams = shown.reduce((sum, job) => sum + (job.filament_used_g ?? 0), 0);
  const completed = shown.filter((job) => job.state === "completed").length;
  const cost = (g: number | null | undefined) => formatCurrency(((g ?? 0) / 1000) * price, "EUR");

  function savePrice(value: string) {
    const next = Number(value);
    if (!(next > 0)) return;
    setPrice(next);
    try {
      localStorage.setItem(PRICE_KEY, String(next));
    } catch {
      // A blocked storage only loses the remembered price.
    }
  }

  const chip = (label: string, active: boolean, onClick: () => void) => (
    <button
      key={label}
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-full border px-3 py-1 text-xs font-medium transition-[color,background-color] duration-press focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
        active
          ? "border-primary bg-accent text-accent-foreground"
          : "border-border bg-card text-foreground hover:bg-muted"
      }`}
    >
      {label}
    </button>
  );

  return (
    <PageContainer>
      <PageHeader
        title={uiText("Print log")}
        description={uiText("Every print, newest first")}
        actions={
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            {uiText("Price per kg")}
            <Input
              type="number"
              min={0}
              step={0.5}
              defaultValue={price}
              onChange={(e) => savePrice(e.target.value)}
              className="h-8 w-20"
            />
            €
          </label>
        }
      />

      <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          [uiText("Prints"), String(shown.length)],
          [
            uiText("Filament"),
            grams >= 1000
              ? `${formatNumber(grams / 1000, { maximumFractionDigits: 2 })} kg`
              : formatGrams(grams),
          ],
          [uiText("Cost"), cost(grams)],
          [
            uiText("Success"),
            shown.length ? formatPercent(Math.round((completed / shown.length) * 100)) : "-",
          ],
        ].map(([label, value]) => (
          <Card key={label} className="px-4 py-3">
            <div className="text-xs text-muted-foreground">{label}</div>
            <div className="mt-0.5 text-xl font-bold tabular-nums">{value}</div>
          </Card>
        ))}
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-2">
        {chip(uiText("Everyone"), person === null, () => setPerson(null))}
        {people.map((name) => chip(name, person === name, () => setPerson(name)))}
        <span className="flex-1" />
        {chip(uiText("This month"), thisMonth, () => setThisMonth(true))}
        {chip(uiText("All time"), !thisMonth, () => setThisMonth(false))}
      </div>

      {jobs.isSuccess && days.length === 0 && (
        <EmptyState icon={Box} title={uiText("No prints yet")} className="mt-10" />
      )}

      {days.map((day) => (
        <section key={day.label} className="mt-6">
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {day.label}
          </h2>
          <Card className="divide-y divide-border overflow-hidden">
            {day.jobs.map((job) => {
              const model = byModel.get(job.model_id);
              const name = model?.name ?? job.remote_filename;
              const canEdit = !!user?.is_superuser && job.printer_id != null;
              return (
                <div key={job.id}>
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
                    <div className="flex min-w-0 flex-1 basis-64 items-center gap-3">
                      <Link
                        to={model ? `/models/${model.id}` : "#"}
                        aria-label={uiText("Open model")}
                        className="shrink-0 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <ProtectedThumbnail
                          path={model?.thumbnail_url}
                          alt={name}
                          className="h-12 w-12 overflow-hidden rounded-md bg-muted"
                          imageClassName="h-full w-full object-cover"
                          placeholder={
                            <Box className="m-auto mt-3 h-6 w-6 text-muted-foreground/40" />
                          }
                        />
                      </Link>
                      <div className="min-w-0">
                        <div className="flex items-center gap-1 font-semibold text-foreground">
                          <span className="truncate">{name}</span>
                          {model && (
                            <Link
                              to={`/models/${model.id}`}
                              aria-label={uiText("Open model")}
                              className="shrink-0 text-muted-foreground hover:text-foreground"
                            >
                              <ArrowUpRight className="h-4 w-4" />
                            </Link>
                          )}
                          {job.state !== "completed" && (
                            <span className="ml-1 text-xs font-bold uppercase text-warning">
                              {uiText(job.state === "failed" ? "Failed" : "Cancelled")}
                            </span>
                          )}
                        </div>
                        <div className="truncate text-xs text-muted-foreground">
                          {model?.collection_label ?? ""}
                        </div>
                        {job.notes && (
                          <div className="text-xs italic text-muted-foreground">{job.notes}</div>
                        )}
                      </div>
                    </div>
                    <div className="w-24 truncate text-sm">{job.recipient ?? "-"}</div>
                    <div className="w-20 text-sm">
                      <SpoolDot name={job.spool_name} />
                    </div>
                    <div className="w-16 text-right text-sm tabular-nums">
                      {formatGrams(job.filament_used_g)}
                    </div>
                    <div className="w-20 text-right text-sm tabular-nums text-muted-foreground">
                      {formatDuration(job.actual_duration_s)}
                    </div>
                    <div className="w-16 text-right text-sm font-semibold tabular-nums">
                      {cost(job.filament_used_g)}
                    </div>
                    {canEdit && (
                      <Button
                        type="button"
                        size="icon-sm"
                        variant="ghost"
                        aria-label={uiText("Edit print")}
                        aria-expanded={editing === job.id}
                        onClick={() => setEditing(editing === job.id ? null : job.id)}
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                    )}
                  </div>
                  {editing === job.id && (
                    <PrintEditor
                      job={job}
                      models={models.data ?? []}
                      people={people}
                      spools={spools}
                      onDone={(saved) => {
                        setEditing(null);
                        if (saved) void jobs.refetch();
                      }}
                    />
                  )}
                </div>
              );
            })}
          </Card>
        </section>
      ))}
    </PageContainer>
  );
}
