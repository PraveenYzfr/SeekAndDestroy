import { NavLink, Route, Routes } from "react-router-dom";
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
