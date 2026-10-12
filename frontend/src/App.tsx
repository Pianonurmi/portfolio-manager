import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect } from "react";
import { Route, Routes } from "react-router";
import { InspectorProvider } from "./components/Inspector";
import { Shell } from "./components/Shell";
import { ToastProvider } from "./components/Toasts";
import { Skeleton } from "./components/ui";
import { api } from "./lib/api";
import type { AuthStatus } from "./lib/types";
import { Auth } from "./screens/Auth";

const Home = lazy(() => import("./screens/Dashboard").then((m) => ({ default: m.Dashboard })));
const WhatsNew = lazy(() => import("./screens/WhatsNew").then((m) => ({ default: m.WhatsNew })));
const Portfolio = lazy(() => import("./screens/Portfolio").then((m) => ({ default: m.Portfolio })));
const NetWorth = lazy(() => import("./screens/NetWorth").then((m) => ({ default: m.NetWorth })));
const CashFlow = lazy(() => import("./screens/CashFlow").then((m) => ({ default: m.CashFlow })));
const Tax = lazy(() => import("./screens/Tax").then((m) => ({ default: m.Tax })));
const Life = lazy(() => import("./screens/Life").then((m) => ({ default: m.Life })));
const Imports = lazy(() => import("./screens/Imports").then((m) => ({ default: m.Imports })));
const Planning = lazy(() => import("./screens/Planning").then((m) => ({ default: m.Planning })));
const Etfs = lazy(() => import("./screens/Etfs").then((m) => ({ default: m.Etfs })));
const Household = lazy(() => import("./screens/Household").then((m) => ({ default: m.Household })));
const Kids = lazy(() => import("./screens/Kids").then((m) => ({ default: m.Kids })));
const Settings = lazy(() => import("./screens/Settings").then((m) => ({ default: m.Settings })));

export function App() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["auth-status"], queryFn: () => api<AuthStatus>("/api/auth/status"), staleTime: Infinity });
  useEffect(() => {
    const onUnauth = () => qc.invalidateQueries({ queryKey: ["auth-status"] });
    window.addEventListener("pm:unauthorized", onUnauth);
    return () => window.removeEventListener("pm:unauthorized", onUnauth);
  }, [qc]);
  if (status.isLoading) return null;
  if (status.isError) return <div className="auth"><p className="err">Cannot reach the server. Check that the stack is running.</p></div>;
  const s = status.data!;
  const signedIn = () => {
    qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth-status" });
    status.refetch();
  };
  if (!s.signed_in) return <Auth setup={s.setup_required} onDone={signedIn} />;
  return (
    <ToastProvider>
      <InspectorProvider>
        <Routes>
          <Route element={<Shell username={s.user?.username ?? ""} open={s.open} />}>
            <Route index element={<Lazy><Home /></Lazy>} />
            <Route path="portfolio" element={<Lazy><Portfolio /></Lazy>} />
            <Route path="net-worth" element={<Lazy><NetWorth /></Lazy>} />
            <Route path="cash-flow" element={<Lazy><CashFlow /></Lazy>} />
            <Route path="tax" element={<Lazy><Tax /></Lazy>} />
            <Route path="life" element={<Lazy><Life /></Lazy>} />
            <Route path="imports/*" element={<Lazy><Imports /></Lazy>} />
            <Route path="planning" element={<Lazy><Planning /></Lazy>} />
            <Route path="etfs" element={<Lazy><Etfs /></Lazy>} />
            <Route path="kids" element={<Lazy><Kids /></Lazy>} />
            <Route path="household" element={<Lazy><Household /></Lazy>} />
            <Route path="settings/*" element={<Lazy><Settings /></Lazy>} />
            <Route path="whats-new" element={<Lazy><WhatsNew /></Lazy>} />
            <Route path="*" element={<p>Not found</p>} />
          </Route>
        </Routes>
      </InspectorProvider>
    </ToastProvider>
  );
}

function Lazy({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<Skeleton w={200} />}>{children}</Suspense>;
}
