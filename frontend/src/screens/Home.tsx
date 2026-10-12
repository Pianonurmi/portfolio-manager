import { useQuery } from "@tanstack/react-query";
import { Link, useLocation } from "react-router";
import { Panel, ScreenHead } from "../components/ui";
import { api } from "../lib/api";
import type { Account, Person } from "../lib/types";

export function Onboarding() {
  const loc = useLocation();
  const people = useQuery({ queryKey: ["persons"], queryFn: () => api<Person[]>("/api/persons") });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/api/accounts") });
  const steps = [
    { done: !!people.data?.length, title: "Add the people in your household", sub: "You, your spouse (shared assets only) and the kids", to: "/settings/people?new=1" },
    { done: !!accounts.data?.length, title: "Add your accounts", sub: "Nordnet AOT and OST, kids' accounts, banks, mortgage, credit card", to: "/settings/accounts?new=1" },
    { done: false, title: "Import your Nordnet transaction history", sub: "Export all transactions from Nordnet and drop the file here", to: "/imports?new=1" },
  ];
  return (
    <>
      <ScreenHead title="Welcome" sub="Three steps to your first net worth figure" />
      <Panel title="Get started" bodyless>
        <div className="steps">
          {steps.map((s, i) => (
            <div key={i} className={`step ${s.done ? "done" : ""}`}>
              <span className="n">{s.done ? "✓" : i + 1}</span>
              <div className="t">
                {s.title}
                <small>{s.sub}</small>
              </div>
              {!s.done && (
                <Link className="btn" to={s.to + (loc.search ? "&" + loc.search.slice(1) : "")}>
                  Start
                </Link>
              )}
            </div>
          ))}
        </div>
      </Panel>
    </>
  );
}
