import { lazy } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import AppLayout from "@/layouts/AppLayout";
import LoginPage from "@/pages/Login";
import { useAuthStore } from "@/store/auth";

const DashboardPage = lazy(() => import("@/pages/Dashboard"));
const WorkbenchPage = lazy(() => import("@/pages/Workbench"));
const PolicyPage = lazy(() => import("@/pages/Policy"));
const SimulationPage = lazy(() => import("@/pages/Simulation"));

function Protected({ children }: { children: React.ReactElement }) {
  const token = useAuthStore((s) => s.token);
  if (!token) return <Navigate to="/login" replace />;
  return children;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/"
          element={
            <Protected>
              <AppLayout />
            </Protected>
          }
        >
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<DashboardPage />} />
          <Route path="workbench" element={<WorkbenchPage />} />
          <Route path="policy" element={<PolicyPage />} />
          <Route path="simulation" element={<SimulationPage />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Route>
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </BrowserRouter>
  );
}