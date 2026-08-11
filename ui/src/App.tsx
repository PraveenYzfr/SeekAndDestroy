import { useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { clearSession, getIdentity, onSessionChange, type Identity } from "@/auth/session";
import Login from "@/pages/Login";
import Chat from "@/pages/Chat";
import Dashboard from "@/pages/Dashboard";
import HostingRecommendation from "@/pages/HostingRecommendation";
import RecommendationComparison from "@/pages/RecommendationComparison";
import ClusterRightSizing from "@/pages/ClusterRightSizing";
import ApplicationPlacement from "@/pages/ApplicationPlacement";
import CapacityForecast from "@/pages/CapacityForecast";
import InvestigationDetail from "@/pages/InvestigationDetail";
import RecommendationApproval from "@/pages/RecommendationApproval";

const NAV = [
  { to: "/", label: "Chat", end: true },
  { to: "/dashboard", label: "Dashboard" },
  { to: "/hosting", label: "Hosting Recommendation" },
  { to: "/compare", label: "Recommendation Comparison" },
  { to: "/right-sizing", label: "Cluster Right-Sizing" },
  { to: "/placement", label: "Application Placement" },
  { to: "/forecast", label: "Capacity Forecast" },
  { to: "/investigations", label: "Investigation Detail" },
  { to: "/approvals", label: "Recommendation Approval" },
];

export default function App() {
  const [identity, setIdentity] = useState<Identity | null>(getIdentity);

  // Any 401 anywhere in the app clears the session, which lands here and
  // swaps the whole shell for the login screen - so an expired token never
  // leaves the user clicking a dead UI.
  useEffect(() => onSessionChange(setIdentity), []);

  if (!identity) {
    return <Login onSignedIn={() => setIdentity(getIdentity())} />;
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <h1>SeekAndDestroy</h1>
        <nav>
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => (isActive ? "active" : "")}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="signed-in-as">
          <div className="stat-label">Signed in as</div>
          <div>{identity.display_name}</div>
          <div className="stat-label">{identity.employee_number}</div>
          <button className="secondary" style={{ marginTop: 8 }} onClick={clearSession}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<Chat />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/hosting" element={<HostingRecommendation />} />
          <Route path="/compare" element={<RecommendationComparison />} />
          <Route path="/right-sizing" element={<ClusterRightSizing />} />
          <Route path="/placement" element={<ApplicationPlacement />} />
          <Route path="/forecast" element={<CapacityForecast />} />
          <Route path="/investigations" element={<InvestigationDetail />} />
          <Route path="/approvals" element={<RecommendationApproval />} />
        </Routes>
      </main>
    </div>
  );
}
