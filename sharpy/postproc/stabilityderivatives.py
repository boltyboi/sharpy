import sharpy.utils.solver_interface as solver_interface
import os
import numpy as np
import scipy.sparse as scsp
import sharpy.linear.src.libsparse as libsp
import sharpy.utils.cout_utils as cout
import sharpy.utils.algebra as algebra
import sharpy.utils.settings as settings
import h5py
import sharpy.utils.h5utils as h5utils


@solver_interface.solver
class StabilityDerivatives(solver_interface.BaseSolver):
    """
    Outputs the stability derivatives of a free-flying aircraft

    Warnings:
        Under Development

    To Do:
        * Coefficient of stability derivatives
        * Option to output in NED frame

    """
    solver_id = 'StabilityDerivatives'
    solver_classification = 'post-processor'

    settings_default = dict()
    settings_description = dict()
    settings_types = dict()
    settings_options = dict()

    settings_types['print_info'] = 'bool'
    settings_default['print_info'] = True
    settings_description['print_info'] = 'Display info to screen'

    settings_types['folder'] = 'str'
    settings_default['folder'] = './output/'
    settings_description['folder'] = 'Output directory'

    settings_types['target_system'] = 'str'
    settings_default['target_system'] = 'aerodynamic'
    settings_description['target_system'] = 'Get rigid (``aerodynamic``) or ``aeroelastic`` derivatives.'
    settings_options['target_system'] = ['aerodynamic', 'aeroelastic']

    settings_types['u_inf'] = 'float'
    settings_default['u_inf'] = 1.
    settings_description['u_inf'] = 'Free stream reference velocity'

    settings_types['S_ref'] = 'float'
    settings_default['S_ref'] = 1.
    settings_description['S_ref'] = 'Reference planform area'

    settings_types['b_ref'] = 'float'
    settings_default['b_ref'] = 1.
    settings_description['b_ref'] = 'Reference span'

    settings_types['c_ref'] = 'float'
    settings_default['c_ref'] = 1.
    settings_description['c_ref'] = 'Reference chord'

    settings_table = settings.SettingsTable()
    __doc__ += settings_table.generate(settings_types, settings_default, settings_description)

    def __init__(self):
        self.data = None
        self.settings = dict()

        self.u_inf = 1
        self.inputs = 0
        self.caller = None

    def initialise(self, data, custom_settings=None, caller=None):
        self.data = data

        if custom_settings:
            self.settings = custom_settings
        else:
            self.settings = self.data.settings[self.solver_id]

        settings.to_custom_types(self.settings, self.settings_types, self.settings_default,
                                 options=self.settings_options,
                                 no_ctype=True)
        self.caller = caller

        u_inf = self.settings['u_inf']
        s_ref = self.settings['S_ref']
        b_ref = self.settings['b_ref']
        c_ref = self.settings['c_ref']
        rho = self.data.linear.tsaero0.rho

        # need to decide whether coefficients stays here or goes just in Derivatives class
        self.coefficients = {'force': 0.5 * rho * u_inf ** 2 * s_ref,
                             'moment_lon': 0.5 * rho * u_inf ** 2 * s_ref * c_ref,
                             'moment_lat': 0.5 * rho * u_inf ** 2 * s_ref * b_ref,
                             'force_angular_vel': 0.5 * rho * u_inf ** 2 * s_ref * c_ref / u_inf,
                             'moment_lon_angular_vel': 0.5 * rho * u_inf ** 2 * s_ref * c_ref * c_ref / u_inf}  # missing rates

        self.data.linear.derivatives = Derivatives(self.coefficients)

    def run(self, online=False):

        # TODO: consider running all required solvers inside this one to keep the correct settings
        # i.e: run Modal, Linear Ass

        derivatives = self.data.linear.derivatives
        Y_freq = self.uvlm_steady_state_transfer_function()
        derivatives.dict_of_derivatives['force_angle'] = self.angle_derivatives(Y_freq)
        derivatives.dict_of_derivatives['force_velocity'] = self.body_derivatives(Y_freq)

        derivatives.save(self.settings['folder'])

        # derivatives_dimensional, derivatives_coeff = self.derivatives(Y_freq)

        # self.export_derivatives(np.hstack((derivatives_coeff[:, :6], derivatives_coeff[:, -2:])))

        return self.data

    def uvlm_steady_state_transfer_function(self):
        """
        Stability derivatives calculated using the transfer function of the UVLM projected onto the structural
        degrees of freedom at zero frequency (steady state).

        Returns:
            np.array: matrix containing the steady state values of the transfer function between the force output
              (columns) and the velocity / control surface inputs (rows).
        """
        if self.settings['target_system'] == 'aerodynamic':
            ss = self.data.linear.linear_system.uvlm.ss
        elif self.settings['target_system'] == 'aeroelastic':
            ss = self.data.linear.ss
        else:
            raise NameError('Unknown target system {:s}'.format(self.settings['target_system']))
        modal = self.data.linear.linear_system.beam.sys.modal
        use_euler = self.data.linear.linear_system.beam.sys.use_euler

        nout = 6
        if use_euler:
            rig_dof = 9
        else:
            rig_dof = 10

        A, B, C, D = ss.get_mats()
        H0 = ss.freqresp(np.array([0.]))[:, :, 0]
        # if type(A) == libsp.csc_matrix:
        #     H0 = C.dot(scsp.linalg.inv(scsp.eye(ss.states, format='csc') - A).dot(B)) + D
        # else:
        #     H0 = C.dot(np.linalg.inv(np.eye(ss.states) - A).dot(B)) + D

        if modal:
            vel_inputs_variables = ss.input_variables.get_variable_from_name('q_dot')
            rbm_indices = vel_inputs_variables.cols_loc

            # look for control surfaces
            try:
                cs_input_variables = ss.input_variables.get_variable_from_name('delta')
            except ValueError:
                cs_indices = np.array([], dtype=int)
            else:
                cs_indices = cs_input_variables.cols_loc
            finally:
                input_indices = np.concatenate((rbm_indices, cs_indices))

            output_indices = ss.output_variables.get_variable_from_name('Q').rows_loc[:6]

            H0 = H0[np.ix_(output_indices, input_indices)].real

            return H0

    def angle_derivatives(self, H0):
        r"""
        Stability derivatives against aerodynamic angles (angle of attack and sideslip) expressed in stability axes, i.e
        forces are lift, drag...

        Linearised forces in stability axes are expressed as

        .. math::
            F^S = F_0^S + \frac{\partial}{\partial \alpha}\left(C^{GA}(\alpha)F_0^A\right)\delta\alpha + C_0^{GA}\delta F^A

        Therefore, the stability derivative becomes

        .. math:: \frac{\partial\F^S}{\partial\alpha} =\frac{\partial}{\partial \alpha}\left(C^{GA}(\alpha)F_0^A\right) +
           C_0^{GA}\frac{\partial F^A}{\partial\alpha}

        where

        .. math:: \frac{\partial F^A}{\partial\alpha} = \frac{\partial F^A}{\partial v^A}\frac{\partial v^A}{\partial\alpha}

        and

        .. math:: \frac{\partial v^A}{\partial\alpha} = C^{AG}\frac{\partial}{\partial\alpha}\left(C(0)V_0^G\right).

        The term :math:`\frac{\partial F^A}{\partial v^A}` is obtained directly from the steady state transfer
        function of the linear UVLM expressed in the beam degrees of freedoms.

        Args:
            H0 (np.ndarray): Steady state gain transfer function of the linear UVLM expressed in the beam rigid
            degrees of freedom

        Returns:
            DerivativeSet: containing the derivatives.
        """
        derivative_set = DerivativeSet('stability')
        derivative_set.labels_in = ['phi', 'alpha', 'beta']
        derivative_set.labels_out = ['CD', 'CY', 'CL', 'Cl', 'Cm', 'Cn']
        derivative_set.matrix = np.zeros((6, 3))

        modal = self.data.linear.linear_system.beam.sys.modal

        # Get free stream velocity direction
        try:
            v0 = self.data.settings['StaticUvlm']['velocity_field_input']['u_inf_direction'] * \
                 self.data.settings['StaticUvlm']['velocity_field_input']['u_inf'] * -1 # aircraft moving fwd in a stat fluid
        except TypeError:
            v0 = self.data.settings['StaticCoupled']['aero_solver_settings']['velocity_field_input']['u_inf_direction'] * \
                 self.data.settings['StaticCoupled']['aero_solver_settings']['velocity_field_input']['u_inf'] * -1 # aircraft moving fwd in a stat fluid

        f0a = self.data.linear.linear_system.linearisation_vectors['forces_aero_beam_dof'][:3].copy()
        m0a = self.data.linear.linear_system.linearisation_vectors['forces_aero_beam_dof'][3:6].copy()

        if modal:
            f0a /= self.data.linear.linear_system.linearisation_vectors['mode_shapes'][-9, 0]
            m0a /= self.data.linear.linear_system.linearisation_vectors['mode_shapes'][-6, 3]

        euler0 = self.data.linear.tsstruct0.euler_angles()
        cga = self.data.linear.tsstruct0.cga()

        # first term in the stability derivative expression
        stab_der_trans = algebra.der_Ceuler_by_v(euler0, f0a)
        # second term in the stability derivative expression
        stab_der_trans2 = cga.dot(H0[:3, :3].real.dot(cga.T.dot(algebra.der_Peuler_by_v(euler0 * 0, v0))))

        stab_der_mom = algebra.der_Ceuler_by_v(euler0, m0a)
        stab_der_mom2 = cga.dot(H0[3:6, :3].real.dot(cga.T.dot(algebra.der_Peuler_by_v(euler0 * 0, v0))))

        if modal:
            stab_der_trans2 /= self.data.linear.linear_system.linearisation_vectors['mode_shapes'][-9, 0] ** 2
            stab_der_mom2 /= (self.data.linear.linear_system.linearisation_vectors['mode_shapes'][-9, 0] *
                              self.data.linear.linear_system.linearisation_vectors['mode_shapes'][-6, 3])

        derivative_set.matrix[:3, :] = (stab_der_trans + stab_der_trans2) / self.coefficients['force']
        derivative_set.matrix[3:6, :] = (stab_der_mom + stab_der_mom2)
        derivative_set.matrix[np.ix_([3, 5]), :] /= self.coefficients['moment_lat']
        derivative_set.matrix[4, :] /= self.coefficients['moment_lon']

        # for debugging and checking purposes at the moment
        derivative_set.print()
        derivative_set.save('force_angle', self.settings['folder'] + '/force_angle')

        print('Angle derivatives - using original stability axes')
        angle_derivative_set = DerivativeSet('stability')
        angle_derivative_set.labels_in = ['phi', 'alpha', 'beta']
        angle_derivative_set.labels_out = ['CD', 'CY', 'CL', 'Cl', 'Cm', 'Cn']
        # These are onto the original stability axes at the linearisation
        # The above take the stability axes to rotate with the perturbation!!
        angles = stab_der_trans / self.coefficients['force']
        angles += cga.dot(H0[:3, 6:9]) / self.data.linear.linear_system.linearisation_vectors['mode_shapes'][-9, 0] / self.coefficients['force']
        mom_angles = stab_der_mom
        mom_angles += cga.dot(H0[3:6, 6:9]) / self.data.linear.linear_system.linearisation_vectors['mode_shapes'][-6, 3]
        mom_angles[np.ix_([0, 2]), :] /= self.coefficients['moment_lat']
        mom_angles[1, :] /= self.coefficients['moment_lon']
        angle_derivative_set.matrix = np.vstack((angles, mom_angles))
        angle_derivative_set.print()

        cout.cout_wrap('Body axes')
        angle_derivative_body = DerivativeSet('body')
        angle_derivative_body.labels_in = ['phi', 'alpha', 'beta']
        angle_derivative_body.labels_out = ['C_XA', 'C_YA', 'C_ZA', 'C_LA', 'C_MA', 'C_NA']
        # These are onto the original stability axes at the linearisation
        # The above take the stability axes to rotate with the perturbation!!
        angles = H0[:3, 6:9] / self.data.linear.linear_system.linearisation_vectors['mode_shapes'][-9, 0] / self.coefficients['force']
        mom_angles = H0[3:6, 6:9] / self.data.linear.linear_system.linearisation_vectors['mode_shapes'][-6, 3]
        mom_angles[np.ix_([0, 2]), :] /= self.coefficients['moment_lat']
        mom_angles[1, :] /= self.coefficients['moment_lon']
        angle_derivative_body.matrix = np.vstack((angles, mom_angles))
        angle_derivative_body.print()

        return derivative_set

    def body_derivatives(self, H0):
        derivative_set = DerivativeSet('body')
        derivative_set.labels_in = ['uA', 'vA', 'wA', 'pA', 'qA', 'rA']
        derivative_set.labels_out = ['C_XA', 'C_YA', 'C_ZA', 'C_LA', 'C_MA', 'C_NA']
        derivative_set.matrix = np.zeros((6, 6))

        modal = self.data.linear.linear_system.beam.sys.modal

        body_derivatives = H0[:6, :6]

        if modal:
            phi = self.data.linear.linear_system.linearisation_vectors['mode_shapes']

            body_derivatives[:3, :3] /= phi[-9, 0] / phi[-9, 0]
            body_derivatives[:3, 3:6] /= phi[-9, 0] / phi[-6, 3]
            body_derivatives[3:6, :3] /= phi[-6, 3] / phi[-9, 0]
            body_derivatives[3:6, 3:6] /= phi[-6, 3] / phi[-6, 3]

        derivative_set.matrix = body_derivatives
        derivative_set.matrix[:3, :] /= self.coefficients['force']
        derivative_set.matrix[np.ix_([3, 5]), :] /= self.coefficients['moment_lat']
        derivative_set.matrix[4, :] /= self.coefficients['moment_lon']
        derivative_set.print()

        return derivative_set


        # # Get rigid body + control surface inputs
        # try:
        #     n_ctrl_sfc = self.data.linear.linear_system.uvlm.control_surface.n_control_surfaces
        # except AttributeError:
        #     n_ctrl_sfc = 0
        #
        # self.inputs = rig_dof + n_ctrl_sfc
        #
        # in_matrix = np.zeros((ssuvlm.inputs, self.inputs))
        # out_matrix = np.zeros((nout, ssuvlm.outputs))
        #
        # if modal:
        #     # Modal scaling
        #     raise NotImplementedError('Not yet implemented in modal space')
        # else:
        #     in_matrix[-self.inputs:, :] = np.eye(self.inputs)
        #     out_matrix[:, -rig_dof:-rig_dof+6] = np.eye(nout)
        #
        # ssuvlm.addGain(in_matrix, where='in')
        # ssuvlm.addGain(out_matrix, where='out')
        #
        # A, B, C, D = ssuvlm.get_mats()
        # if type(A) == libsp.csc_matrix:
        #     Y_freq = C.dot(scsp.linalg.inv(scsp.eye(ssuvlm.states, format='csc') - A).dot(B)) + D
        # else:
        #     Y_freq = C.dot(np.linalg.inv(np.eye(ssuvlm.states) - A).dot(B)) + D
        # Yf = ssuvlm.freqresp(np.array([0]))
        #
        # return Y_freq

    def a_derivatives(self, Y_freq):

        Cng = np.array([[-1, 0, 0], [0, 1, 0], [0, 0, -1]])  # Project SEU on NED - TODO implementation
        u_inf = self.settings['u_inf'].value
        s_ref = self.settings['S_ref'].value
        b_ref = self.settings['b_ref'].value
        c_ref = self.settings['c_ref'].value
        rho = self.data.linear.tsaero0.rho

        # Inertial frame
        try:
            euler = self.data.linear.tsstruct0.euler
            Pga = algebra.euler2rot(euler)
            rig_dof = 9
        except AttributeError:
            quat = self.data.linear.tsstruct0.quat
            Pga = algebra.quat2rotation(quat)
            rig_dof = 10

        derivatives_g = np.zeros((6, Y_freq.shape[1] + 2))
        coefficients = {'force': 0.5*rho*u_inf**2*s_ref,
                        'moment_lon': 0.5*rho*u_inf**2*s_ref*c_ref,
                        'moment_lat': 0.5*rho*u_inf**2*s_ref*b_ref,
                        'force_angular_vel': 0.5*rho*u_inf**2*s_ref*c_ref/u_inf,
                        'moment_lon_angular_vel': 0.5*rho*u_inf**2*s_ref*c_ref*c_ref/u_inf} # missing rates

        for in_channel in range(Y_freq.shape[1]):
            derivatives_g[:3, in_channel] = Pga.dot(Y_freq[:3, in_channel])
            derivatives_g[3:, in_channel] = Pga.dot(Y_freq[3:, in_channel])

        derivatives_g[:3, :3] /= coefficients['force']
        derivatives_g[:3, 3:6] /= coefficients['force_angular_vel']
        derivatives_g[4, :3] /= coefficients['moment_lon']
        derivatives_g[4, 3:6] /= coefficients['moment_lon_angular_vel']
        derivatives_g[[3, 5], :] /= coefficients['moment_lat']

        derivatives_g[:, -2] = derivatives_g[:, 2] * u_inf  # ders wrt alpha
        derivatives_g[:, -1] = derivatives_g[:, 1] * u_inf  # ders wrt beta

        der_matrix = np.zeros((6, self.inputs - (rig_dof - 6)))
        der_col = 0
        for i in list(range(6))+list(range(rig_dof, self.inputs)):
            der_matrix[:3, der_col] = Y_freq[:3, i]
            der_matrix[3:6, der_col] = Y_freq[3:6, i]
            der_col += 1

        labels_force = {0: 'X',
                        1: 'Y',
                        2: 'Z',
                        3: 'L',
                        4: 'M',
                        5: 'N'}

        labels_velocity = {0: 'u',
                           1: 'v',
                           2: 'w',
                           3: 'p',
                           4: 'q',
                           5: 'r',
                           6: 'flap1',
                           7: 'flap2',
                           8: 'flap3'}

        table = cout.TablePrinter(n_fields=7, field_length=12, field_types=['s', 'f', 'f', 'f', 'f', 'f', 'f'])
        table.print_header(['der'] + list(labels_force.values()))
        for i in range(der_matrix.shape[1]):
            table.print_line([labels_velocity[i]] + list(der_matrix[:, i]))

        table_coeff = cout.TablePrinter(n_fields=7, field_length=12, field_types=['s']+6*['f'])
        labels_out = {0: 'C_D',
                      1: 'C_Y',
                      2: 'C_L',
                      3: 'C_l',
                      4: 'C_m',
                      5: 'C_n'}
        labels_der = {0: 'u',
                           1: 'v',
                           2: 'w',
                           3: 'p',
                           4: 'q',
                           5: 'r',
                      6: 'alpha',
                      7: 'beta'}
        table_coeff.print_header(['der'] + list(labels_out.values()))
        for i in range(6):
            table_coeff.print_line([labels_der[i]] + list(derivatives_g[:, i]))
        table_coeff.print_line([labels_der[6]] + list(derivatives_g[:, -2]))
        table_coeff.print_line([labels_der[7]] + list(derivatives_g[:, -1]))

        return der_matrix, derivatives_g

    def export_derivatives(self, der_matrix_g):

        folder = self.settings['folder'] + '/' + self.data.settings['SHARPy']['case'] + '/stability/'
        if not os.path.exists(folder):
            os.makedirs(folder)
        filename = 'stability_derivatives.txt'

        u_inf = self.settings['u_inf'].value
        s_ref = self.settings['S_ref'].value
        b_ref = self.settings['b_ref'].value
        c_ref = self.settings['c_ref'].value
        rho = self.data.linear.tsaero0.rho
        euler_orient = algebra.quat2euler(self.data.linear.tsstruct0.quat) * 180/np.pi

        labels_der = {0: 'u',
                           1: 'v',
                           2: 'w',
                           3: 'p',
                           4: 'q',
                           5: 'r',
                      6: 'alpha',
                      7: 'beta'}

        labels_out = {0: 'C_D',
                      1: 'C_Y',
                      2: 'C_L',
                      3: 'C_l',
                      4: 'C_m',
                      5: 'C_n'}

        separator = '\n' + 80*'#' + '\n'

        with open(folder + '/' + filename, mode='w') as outfile:
            outfile.write('SHARPy Stability Derivatives Analysis\n')

            outfile.write('State:\n')
            outfile.write('\t%.4f\t\t\t # Free stream velocity\n' % u_inf)
            outfile.write('\t%.4f\t\t\t # Free stream density\n' % rho)
            outfile.write('\t%.4f\t\t\t # Alpha [deg]\n' % euler_orient[1])
            outfile.write('\t%.4f\t\t\t # Beta [deg]\n' % euler_orient[2])

            outfile.write(separator)
            outfile.write('\nReference Dimensions:\n')
            outfile.write('\t%.4f\t\t\t # Reference planform area\n' % s_ref)
            outfile.write('\t%.4f\t\t\t # Reference chord\n' % c_ref)
            outfile.write('\t%.4f\t\t\t # Reference span\n' % b_ref)

            outfile.write(separator)
            outfile.write('\nCoefficients:\n')
            coeffs = self.static_state()
            for i in range(3):
                outfile.write('\t%.4f\t\t\t # %s\n' % (coeffs[i], labels_out[i]))

            outfile.write(separator)
            for k, v in labels_der.items():
                outfile.write('%s derivatives:\n' % v)
                for i in range(6):
                    outfile.write('\t%.4f\t\t\t # %s_%s derivative\n' % (der_matrix_g[i, k], labels_out[i], labels_der[k]))
                outfile.write(separator)

    def steady_aero_forces(self):
        fx = np.sum(self.data.aero.timestep_info[0].inertial_steady_forces[:, 0], 0) + \
             np.sum(self.data.aero.timestep_info[0].inertial_unsteady_forces[:, 0], 0)

        fy = np.sum(self.data.aero.timestep_info[0].inertial_steady_forces[:, 1], 0) + \
             np.sum(self.data.aero.timestep_info[0].inertial_unsteady_forces[:, 1], 0)

        fz = np.sum(self.data.aero.timestep_info[0].inertial_steady_forces[:, 2], 0) + \
             np.sum(self.data.aero.timestep_info[0].inertial_unsteady_forces[:, 2], 0)

        return fx, fy, fz

    def static_state(self):
        fx, fy, fz = self.steady_aero_forces()
        force_coeff = 0.5 * self.data.linear.tsaero0.rho * self.settings['u_inf'].value ** 2 * self.settings['S_ref'].value
        Cfx = fx / force_coeff
        Cfy = fy / force_coeff
        Cfz = fz / force_coeff

        return Cfx, Cfy, Cfz


class Derivatives:

    def __init__(self, coefficients):

        self.coefficients = coefficients

        self.matrix = None

        self.labels_der = {0: 'phi',
                      1: 'alpha',
                      2: 'beta',
                      3: 'p',
                      4: 'q',
                      5: 'r',
                      6: 'alpha',
                      7: 'beta'}

        self.labels_out = {0: 'C_D',
                      1: 'C_Y',
                      2: 'C_L',
                      3: 'C_l',
                      4: 'C_m',
                      5: 'C_n'}

        self.separator = '\n' + 80*'#' + '\n'

        self.angles = None
        self.dict_of_derivatives = {}

    def save(self, output_route):
        with h5py.File(output_route + '/stability.h5', 'w') as f:
            for k, v in self.dict_of_derivatives.items():
                f.create_dataset(name=k, data=v.matrix)


class DerivativeSet:

    def __init__(self, frame_of_reference):

        self.matrix = None
        self.labels_in = []
        self.labels_out = []
        self.frame_of_reference = frame_of_reference

        self.table = None

    def print(self):
        self.table = cout.TablePrinter(n_fields=len(self.labels_in)+1,
                                       field_types=['s']+len(self.labels_in) * ['e'])
        self.table.print_header(field_names=list(['der'] + self.labels_in))
        for i in range(len(self.labels_out)):
            out_list = [self.labels_out[i]] + list(self.matrix[i, :])
            self.table.print_line(out_list)
        self.table.print_divider_line()

    def save(self, derivative_name, output_name):
        with h5py.File(output_name + '.stability.h5', 'w') as f:
            f.create_dataset(derivative_name, data=self.matrix)
